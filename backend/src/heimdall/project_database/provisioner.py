from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from heimdall.project_database.models import ProjectDatabaseProvisioningError


class PostgresProjectDatabaseProvisioner:
    def __init__(self, admin_url: str) -> None:
        self._admin_url = admin_url

    def ensure_role(self, resource_id: UUID, role_name: str, password: str) -> None:
        marker = _marker(resource_id)
        try:
            with psycopg.connect(self._admin_url) as connection:
                row = connection.execute(
                    """
                    SELECT rolname, shobj_description(oid, 'pg_authid') AS marker
                    FROM pg_roles WHERE rolname = %s
                    """,
                    (role_name,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        sql.SQL(
                            """
                            CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
                            NOCREATEROLE NOREPLICATION NOBYPASSRLS
                            """
                        ).format(sql.Identifier(role_name))
                    )
                    connection.execute(
                        sql.SQL("COMMENT ON ROLE {} IS {}").format(
                            sql.Identifier(role_name), sql.Literal(marker)
                        )
                    )
                elif row[1] != marker:
                    raise ProjectDatabaseProvisioningError("ROLE", "OWNERSHIP_CONFLICT")
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role_name), sql.Literal(_scram_verifier(password))
                    )
                )
        except ProjectDatabaseProvisioningError:
            raise
        except psycopg.Error as error:
            raise ProjectDatabaseProvisioningError("ROLE", "ROLE_ENSURE_FAILED") from error

    def ensure_database(self, resource_id: UUID, database_name: str) -> None:
        marker = _marker(resource_id)
        try:
            with psycopg.connect(self._admin_url, autocommit=True) as connection:
                admin_role = connection.execute("SELECT current_user").fetchone()[0]
                row = connection.execute(
                    """
                    SELECT pg_get_userbyid(datdba) AS owner,
                           shobj_description(oid, 'pg_database') AS marker
                    FROM pg_database WHERE datname = %s
                    """,
                    (database_name,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        sql.SQL(
                            "CREATE DATABASE {} OWNER {} TEMPLATE template0 ENCODING 'UTF8'"
                        ).format(sql.Identifier(database_name), sql.Identifier(admin_role))
                    )
                    connection.execute(
                        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                            sql.Identifier(database_name), sql.Literal(marker)
                        )
                    )
                elif row[0] != admin_role or row[1] != marker:
                    raise ProjectDatabaseProvisioningError("DATABASE", "OWNERSHIP_CONFLICT")
        except ProjectDatabaseProvisioningError:
            raise
        except psycopg.Error as error:
            raise ProjectDatabaseProvisioningError("DATABASE", "DATABASE_ENSURE_FAILED") from error

    def ensure_privileges(self, database_name: str, role_name: str, schema_name: str) -> None:
        try:
            with psycopg.connect(self._admin_url, autocommit=True) as connection:
                connection.execute(
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                        sql.Identifier(database_name)
                    )
                )
                connection.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        sql.Identifier(database_name), sql.Identifier(role_name)
                    )
                )
                connection.execute(
                    sql.SQL(
                        "ALTER ROLE {} IN DATABASE {} SET search_path TO {}, pg_catalog"
                    ).format(
                        sql.Identifier(role_name),
                        sql.Identifier(database_name),
                        sql.Identifier(schema_name),
                    )
                )
            with psycopg.connect(_target_url(self._admin_url, database_name)) as connection:
                connection.execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name))
                )
                connection.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
                connection.execute(
                    sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                        sql.Identifier(schema_name), sql.Identifier(role_name)
                    )
                )
                connection.execute(sql.SQL("ALTER ROLE {} LOGIN").format(sql.Identifier(role_name)))
        except psycopg.Error as error:
            raise ProjectDatabaseProvisioningError(
                "PRIVILEGES", "PRIVILEGE_ENSURE_FAILED"
            ) from error

    def verify_login(
        self, database_name: str, role_name: str, schema_name: str, password: str
    ) -> None:
        probe_name = f"heimdall_probe_{uuid4().hex}"
        try:
            connection_url = _target_url(
                self._admin_url, database_name, user=role_name, password=password
            )
            with psycopg.connect(connection_url) as connection:
                connection.execute(
                    sql.SQL("CREATE TABLE {}.{} (id integer)").format(
                        sql.Identifier(schema_name), sql.Identifier(probe_name)
                    )
                )
                connection.rollback()
        except psycopg.Error as error:
            raise ProjectDatabaseProvisioningError("LOGIN", "LOGIN_PROBE_FAILED") from error


def _marker(resource_id: UUID) -> str:
    return f"heimdall-project-database:{resource_id}"


def _target_url(admin_url: str, database_name: str, **overrides: str) -> str:
    values = conninfo_to_dict(admin_url)
    values.update({"dbname": database_name, **overrides})
    return make_conninfo(**values)


def _scram_verifier(password: str) -> str:
    iterations = 4096
    salt = secrets.token_bytes(16)
    salted_password = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted_password, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted_password, b"Server Key", hashlib.sha256).digest()
    encoded_salt = base64.b64encode(salt).decode("ascii")
    encoded_stored = base64.b64encode(stored_key).decode("ascii")
    encoded_server = base64.b64encode(server_key).decode("ascii")
    return f"SCRAM-SHA-256${iterations}:{encoded_salt}${encoded_stored}:{encoded_server}"
