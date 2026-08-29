from uuid import uuid4

import psycopg
import pytest

from heimdall.project_database.models import ProjectDatabaseProvisioningError
from heimdall.project_database.provisioner import (
    PostgresProjectDatabaseProvisioner,
    _scram_verifier,
)


def test_scram_verifier_does_not_contain_raw_password() -> None:
    password = "raw-password-canary"

    verifier = _scram_verifier(password)

    assert verifier.startswith("SCRAM-SHA-256$4096:")
    assert password not in verifier


class _Cursor:
    def __init__(self, row=None) -> None:
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, rows: list[object]) -> None:
        self.rows = list(rows)
        self.statements: list[str] = []

    def execute(self, statement, _parameters=None):
        rendered = str(statement)
        self.statements.append(rendered)
        row = (
            self.rows.pop(0)
            if rendered.lstrip().upper().startswith("SELECT") and self.rows
            else None
        )
        return _Cursor(row)

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def test_database_operation_lock_wraps_the_entire_external_operation(monkeypatch) -> None:
    connection = _Connection([None, None])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    provisioner = PostgresProjectDatabaseProvisioner("postgresql://admin/control")

    with provisioner.operation_lock(uuid4()):
        connection.statements.append("EXTERNAL_OPERATION")

    lock_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "pg_advisory_lock" in statement
    )
    unlock_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "pg_advisory_unlock" in statement
    )
    assert lock_index < connection.statements.index("EXTERNAL_OPERATION") < unlock_index


def test_database_operation_lock_sets_bounded_statement_and_lock_timeouts(monkeypatch) -> None:
    connection = _Connection([None, None])
    connection_options = {}

    def connect(*_args, **kwargs):
        connection_options.update(kwargs)
        return connection

    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        connect,
    )

    with PostgresProjectDatabaseProvisioner("postgresql://admin/control").operation_lock(uuid4()):
        pass

    assert any("statement_timeout" in statement for statement in connection.statements)
    assert any("lock_timeout" in statement for statement in connection.statements)
    assert connection_options["connect_timeout"] == 10


def test_nonblocking_database_operation_lock_reports_busy_resource(monkeypatch) -> None:
    connection = _Connection([(False,)])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    with (
        pytest.raises(ProjectDatabaseProvisioningError) as raised,
        PostgresProjectDatabaseProvisioner("postgresql://admin/control").operation_lock(
            uuid4(), blocking=False
        ),
    ):
        pass

    assert raised.value.code == "OPERATION_LOCK_BUSY"


def test_database_deletion_preflight_requires_database_role_and_session_privileges(
    monkeypatch,
) -> None:
    connection = _Connection([("admin", True, False, True)])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(ProjectDatabaseProvisioningError) as raised:
        PostgresProjectDatabaseProvisioner("postgresql://admin/control").preflight_deletion()

    assert raised.value.stage == "DELETE"
    assert raised.value.code == "PREFLIGHT_PRIVILEGES_MISSING"
    assert any("pg_signal_backend" in statement for statement in connection.statements)


def test_database_deletion_preflight_requires_signal_set_privilege(monkeypatch) -> None:
    connection = _Connection([("admin", True, True, True)])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    PostgresProjectDatabaseProvisioner("postgresql://admin/control").preflight_deletion()

    privilege_query = next(
        statement for statement in connection.statements if "pg_signal_backend" in statement
    )
    assert "'SET'" in privilege_query
    assert "'MEMBER'" not in privilege_query


def test_database_quiesce_activates_noinherit_signal_role(monkeypatch) -> None:
    resource_id = uuid4()
    marker = f"heimdall-project-database:{resource_id}"
    connection = _Connection(
        [
            ("project_role", marker),
            ("admin", marker, "admin"),
            (True,),
            (0,),
        ]
    )
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    PostgresProjectDatabaseProvisioner("postgresql://admin/control").quiesce(
        resource_id, "project_database", "project_role"
    )

    set_role_index = connection.statements.index("SET ROLE pg_signal_backend")
    terminate_index = next(
        index
        for index, statement in enumerate(connection.statements)
        if "pg_terminate_backend" in statement
    )
    reset_role_index = connection.statements.index("RESET ROLE")
    assert set_role_index < terminate_index < reset_role_index


def test_database_quiesce_resets_signal_role_when_termination_fails(monkeypatch) -> None:
    resource_id = uuid4()
    marker = f"heimdall-project-database:{resource_id}"

    class FailingTerminateConnection(_Connection):
        def execute(self, statement, parameters=None):
            rendered = str(statement)
            if "pg_terminate_backend" in rendered:
                self.statements.append(rendered)
                raise psycopg.OperationalError("termination failed")
            return super().execute(statement, parameters)

    connection = FailingTerminateConnection([("project_role", marker), ("admin", marker, "admin")])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(ProjectDatabaseProvisioningError) as raised:
        PostgresProjectDatabaseProvisioner("postgresql://admin/control").quiesce(
            resource_id, "project_database", "project_role"
        )

    assert raised.value.stage == "DELETE"
    assert raised.value.code == "QUIESCE_FAILED"
    assert "RESET ROLE" in connection.statements


def test_database_quiesce_preserves_resources_when_role_marker_conflicts(monkeypatch) -> None:
    resource_id = uuid4()
    connection = _Connection([("project_role", "another-owner")])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    provisioner = PostgresProjectDatabaseProvisioner("postgresql://admin/control")

    with pytest.raises(ProjectDatabaseProvisioningError) as raised:
        provisioner.quiesce(resource_id, "project_database", "project_role")

    assert raised.value.stage == "DELETE"
    assert raised.value.code == "OWNERSHIP_CONFLICT"
    assert any("statement_timeout" in statement for statement in connection.statements)
    assert any("lock_timeout" in statement for statement in connection.statements)


def test_database_drop_is_idempotent_after_lost_acknowledgement(monkeypatch) -> None:
    connection = _Connection([None, None])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )
    provisioner = PostgresProjectDatabaseProvisioner("postgresql://admin/control")

    provisioner.drop_database(uuid4(), "already_absent")
    provisioner.drop_role(uuid4(), "already_absent")

    assert not any(
        "DROP DATABASE" in statement or "DROP ROLE" in statement
        for statement in connection.statements
    )


def test_database_final_absence_verification_rejects_reappeared_owned_resource(
    monkeypatch,
) -> None:
    resource_id = uuid4()
    marker = f"heimdall-project-database:{resource_id}"
    connection = _Connection([("admin", marker, "admin"), None])
    monkeypatch.setattr(
        "heimdall.project_database.provisioner.psycopg.connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(ProjectDatabaseProvisioningError) as raised:
        PostgresProjectDatabaseProvisioner("postgresql://admin/control").verify_absent(
            resource_id, "project_database", "project_role"
        )

    assert raised.value.code == "RESOURCES_REAPPEARED"
