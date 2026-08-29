from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import timedelta
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import pytest
from conftest import FakeGit

from heimdall.database import Database
from heimdall.projects.repository import PostgresProjectRepository
from heimdall.projects.schemas import ProjectCreate
from heimdall.projects.service import ProjectService
from heimdall.public_routes.repository import PostgresPublicRouteRepository
from heimdall.public_routes.worker import PublicRouteWorker
from heimdall.runtime.edge import (
    DockerEdgeConfigManager,
    EdgeRouteProbe,
    render_edge_routes,
)
from heimdall.runtime.edge_network import DockerEdgeNetworkConnector
from heimdall.runtime.gateway_identity import project_gateway_name
from heimdall.runtime.process import CommandResult, SubprocessCommandRunner

CONTROL_URL = os.environ.get("HEIMDALL_TEST_CONTROL_DB_URL")
EDGE_NETWORK = os.environ.get("HEIMDALL_TEST_EDGE_NETWORK")
EDGE_CONTAINER = os.environ.get("HEIMDALL_TEST_EDGE_CONTAINER")
EDGE_CONFIG_ROOT = os.environ.get("HEIMDALL_TEST_EDGE_CONFIG_ROOT")
TEST_ID = os.environ.get("HEIMDALL_TEST_ID")
EDGE_HOST = os.environ.get("HEIMDALL_TEST_EDGE_HOST", "127.0.0.1")
EDGE_PORT = int(os.environ.get("HEIMDALL_TEST_EDGE_PORT", "18088"))
MANAGEMENT_HOSTNAME = os.environ.get(
    "HEIMDALL_TEST_MANAGEMENT_HOSTNAME", "control.routing-smoke.test"
)
_EDGE_CONFIG_OWNER_MARKER = ".heimdall-routing-smoke-owner"
_EDGE_CONFIG_ALLOWED_ENTRIES = {
    _EDGE_CONFIG_OWNER_MARKER,
    ".routing.lock",
    "public-routes.conf",
}
_EDGE_CONFIG_NO_ROUTE_STATES = {
    "# No active public project routes.\n",
    render_edge_routes([]),
}

_docker_smoke_unconfigured = (
    os.environ.get("HEIMDALL_RUN_DOCKER_SMOKE") != "true"
    or not CONTROL_URL
    or not EDGE_NETWORK
    or not EDGE_CONTAINER
    or not EDGE_CONFIG_ROOT
    or not TEST_ID
)


@pytest.mark.skipif(
    _docker_smoke_unconfigured,
    reason="Public hostname Docker smoke is not configured",
)
def test_two_public_hostnames_are_isolated_and_survive_gateway_recreation(
    tmp_path: Path,
) -> None:
    assert CONTROL_URL is not None
    assert EDGE_NETWORK is not None
    assert EDGE_CONTAINER is not None
    assert EDGE_CONFIG_ROOT is not None
    assert TEST_ID is not None
    _assert_test_database_url(CONTROL_URL)
    edge_config_root = _assert_test_edge_config_root(EDGE_CONFIG_ROOT, TEST_ID)
    runner = SubprocessCommandRunner(heartbeat_interval_seconds=1)
    _assert_test_edge_scope(
        runner,
        edge_config_root,
        edge_network=EDGE_NETWORK,
        edge_container=EDGE_CONTAINER,
        test_id=TEST_ID,
    )
    database = Database(CONTROL_URL)
    database.open()
    try:
        _assert_empty_control_database(database)
    except BaseException:
        database.close()
        raise
    database_open = True
    gateways: list[tuple[UUID, UUID, str]] = []
    project_ids: list[UUID] = []
    edge_config = _edge_manager(runner, edge_config_root)
    try:
        projects = ProjectService(PostgresProjectRepository(database), FakeGit())
        routes = PostgresPublicRouteRepository(database)
        run_id = uuid4().hex[:8]
        project_a = projects.create(
            ProjectCreate(
                name=f"Routing-smoke-a-{run_id}",
                repositoryUrl=f"https://github.com/example/routing-smoke-a-{run_id}",
            )
        )
        project_ids.append(project_a.id)
        project_b = projects.create(
            ProjectCreate(
                name=f"Routing-smoke-b-{run_id}",
                repositoryUrl=f"https://github.com/example/routing-smoke-b-{run_id}",
            )
        )
        project_ids.append(project_b.id)
        deployment_a = uuid4()
        deployment_b = uuid4()
        gateway_a = _start_gateway(
            runner,
            tmp_path,
            project_a.id,
            deployment_a,
            "project-a",
        )
        gateways.append((project_a.id, deployment_a, gateway_a))
        gateway_b = _start_gateway(
            runner,
            tmp_path,
            project_b.id,
            deployment_b,
            "project-b",
        )
        gateways.append((project_b.id, deployment_b, gateway_b))
        stable_port_b = _published_port(runner, gateway_b)

        hostname_a = f"student-a-{run_id}.deployments.routing-smoke.test"
        hostname_b = f"student-b-{run_id}.deployments.routing-smoke.test"
        routes.set_enabled(project_a.id, f"student-a-{run_id}", hostname_a)
        routes.set_enabled(project_b.id, f"student-b-{run_id}", hostname_b)
        worker = _worker(routes, runner, edge_config)

        assert worker.run_once() is True
        assert worker.run_once() is True

        response_a = _request(hostname_a)
        response_b = _request(hostname_b)
        assert response_a == (200, str(deployment_a), b"project-a\n")
        assert response_b == (200, str(deployment_b), b"project-b\n")
        assert _request(f"unknown-{run_id}.deployments.routing-smoke.test")[0] == 404
        assert _request(f"bad_{run_id}.deployments.routing-smoke.test")[0] == 404
        assert _request(f"student-a-{run_id}.other-routing-smoke.test")[0] == 404
        _assert_edge_alias(runner, gateway_a, project_a.id)
        _assert_edge_alias(runner, gateway_b, project_b.id)

        _restart_gateway_exact(runner, gateway_b, project_b.id)
        _wait_for_response(hostname_b, (200, str(deployment_b), b"project-b\n"))

        _remove_gateway_exact(runner, gateway_b, project_b.id)
        _remove_generation_network_exact(runner, project_b.id, deployment_b)
        gateways.remove((project_b.id, deployment_b, gateway_b))
        replacement_deployment = uuid4()
        replacement_gateway = _start_gateway(
            runner,
            tmp_path,
            project_b.id,
            replacement_deployment,
            "project-b-replaced",
            stable_port=stable_port_b,
        )
        gateways.append((project_b.id, replacement_deployment, replacement_gateway))

        assert worker.reconcile_startup() is True
        assert _published_port(runner, replacement_gateway) == stable_port_b
        assert _request(hostname_b) == (
            200,
            str(replacement_deployment),
            b"project-b-replaced\n",
        )

        routes.disable(project_a.id)
        assert worker.run_once() is True
        assert _request(hostname_a)[0] == 404
        assert _request(hostname_b)[1] == str(replacement_deployment)
        assert _request_management() == (200, "true")
        database.close()
        database_open = False
        assert _request(hostname_b)[1] == str(replacement_deployment)
    finally:
        try:
            edge_config.apply(
                [],
                None,
                heartbeat=lambda: None,
                fence=lambda: None,
                finalize=lambda: None,
            )
        finally:
            try:
                for project_id, deployment_id, gateway_name in reversed(gateways):
                    _remove_gateway_exact(runner, gateway_name, project_id)
                    _remove_generation_network_exact(runner, project_id, deployment_id)
            finally:
                if database_open:
                    database.close()
                _delete_test_projects(CONTROL_URL, project_ids)


def _worker(routes, runner, edge_config: DockerEdgeConfigManager) -> PublicRouteWorker:
    assert EDGE_NETWORK is not None
    assert EDGE_CONTAINER is not None
    assert EDGE_CONFIG_ROOT is not None
    return PublicRouteWorker(
        routes,
        DockerEdgeNetworkConnector(
            runner,
            network_name=EDGE_NETWORK,
            command_timeout_seconds=30,
        ),
        edge_config,
        worker_id="public-routing-smoke",
        lease_duration=timedelta(seconds=30),
    )


def _edge_manager(
    runner: SubprocessCommandRunner,
    config_root: Path,
) -> DockerEdgeConfigManager:
    assert EDGE_NETWORK is not None
    assert EDGE_CONTAINER is not None
    return DockerEdgeConfigManager(
        runner,
        EdgeRouteProbe(EDGE_HOST, EDGE_PORT, timeout_seconds=10),
        config_root,
        MANAGEMENT_HOSTNAME,
        edge_network_name=EDGE_NETWORK,
        edge_container_name=EDGE_CONTAINER,
        command_timeout_seconds=30,
    )


def _start_gateway(
    runner: SubprocessCommandRunner,
    root: Path,
    project_id: UUID,
    deployment_id: UUID,
    body: str,
    *,
    stable_port: int | None = None,
) -> str:
    network_name = _generation_network_name(project_id, deployment_id)
    gateway_name = project_gateway_name(project_id)
    directory = root / deployment_id.hex
    directory.mkdir(mode=0o700)
    (directory / "default.conf").write_text(
        "\n".join(
            [
                "server {",
                "    listen 8080;",
                f'    add_header X-Heimdall-Deployment-Id "{deployment_id}" always;',
                "    location / {",
                "        default_type text/plain;",
                f'        return 200 "{body}\\n";',
                "    }",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    runner.run(
        [
            "docker",
            "network",
            "create",
            "--label",
            "heimdall.managed=true",
            "--label",
            "heimdall.kind=routing-smoke-network",
            "--label",
            f"heimdall.project-id={project_id}",
            "--label",
            f"heimdall.deployment-id={deployment_id}",
            "--label",
            f"heimdall.test-id={TEST_ID}",
            network_name,
        ],
        timeout_seconds=30,
    )
    published = f"127.0.0.1:{stable_port or ''}:8080"
    runner.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            gateway_name,
            "--network",
            network_name,
            "--publish",
            published,
            "--restart",
            "unless-stopped",
            "--label",
            "heimdall.managed=true",
            "--label",
            f"heimdall.project-id={project_id}",
            "--label",
            "heimdall.kind=gateway",
            "--label",
            f"heimdall.test-id={TEST_ID}",
            "--mount",
            f"type=bind,src={directory},dst=/etc/nginx/conf.d,readonly",
            "nginx:1.29-alpine",
        ],
        timeout_seconds=30,
    )
    return gateway_name


def _request(hostname: str) -> tuple[int, str | None, bytes]:
    connection = HTTPConnection(EDGE_HOST, EDGE_PORT, timeout=5)
    connection.request("GET", "/", headers={"Host": hostname})
    response = connection.getresponse()
    result = (
        response.status,
        response.getheader("X-Heimdall-Deployment-Id"),
        response.read(),
    )
    connection.close()
    return result


def _request_management() -> tuple[int, str | None]:
    connection = HTTPConnection(EDGE_HOST, EDGE_PORT, timeout=5)
    connection.request("GET", "/", headers={"Host": MANAGEMENT_HOSTNAME})
    response = connection.getresponse()
    result = (response.status, response.getheader("X-Heimdall-Management"))
    response.read()
    connection.close()
    return result


def _published_port(runner: SubprocessCommandRunner, gateway_name: str) -> int:
    output = runner.run(
        ["docker", "port", gateway_name, "8080/tcp"],
        timeout_seconds=30,
    ).stdout.strip()
    return int(output.rsplit(":", 1)[1])


def _assert_edge_alias(
    runner: SubprocessCommandRunner,
    gateway_name: str,
    project_id: UUID,
) -> None:
    assert EDGE_NETWORK is not None
    output = runner.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            gateway_name,
        ],
        timeout_seconds=30,
    ).stdout
    networks = json.loads(output)
    assert project_gateway_name(project_id) in networks[EDGE_NETWORK]["Aliases"]


def _restart_gateway_exact(
    runner: SubprocessCommandRunner,
    gateway_name: str,
    project_id: UUID,
) -> None:
    _assert_gateway_labels(runner, gateway_name, project_id)
    runner.run(["docker", "stop", gateway_name], timeout_seconds=30)
    _assert_gateway_labels(runner, gateway_name, project_id)
    runner.run(["docker", "start", gateway_name], timeout_seconds=30)


def _wait_for_response(
    hostname: str,
    expected: tuple[int, str | None, bytes],
) -> None:
    deadline = time.monotonic() + 10
    observed = _request(hostname)
    while observed != expected and time.monotonic() < deadline:
        time.sleep(0.1)
        observed = _request(hostname)
    assert observed == expected


def _remove_gateway_exact(
    runner: SubprocessCommandRunner,
    gateway_name: str,
    project_id: UUID,
) -> None:
    observed = runner.run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", gateway_name],
        timeout_seconds=30,
        check=False,
    )
    if observed.returncode != 0:
        return
    _assert_gateway_labels(runner, gateway_name, project_id, observed.stdout)
    runner.run(["docker", "rm", "--force", gateway_name], timeout_seconds=30)


def _assert_gateway_labels(
    runner: SubprocessCommandRunner,
    gateway_name: str,
    project_id: UUID,
    observed_labels: str | None = None,
) -> None:
    if observed_labels is None:
        observed = runner.run(
            ["docker", "inspect", "--format", "{{json .Config.Labels}}", gateway_name],
            timeout_seconds=30,
        )
        observed_labels = observed.stdout
    labels = json.loads(observed_labels)
    assert labels.get("heimdall.managed") == "true"
    assert labels.get("heimdall.project-id") == str(project_id)
    assert labels.get("heimdall.kind") == "gateway"
    assert labels.get("heimdall.test-id") == TEST_ID


def _remove_generation_network_exact(
    runner: SubprocessCommandRunner,
    project_id: UUID,
    deployment_id: UUID,
) -> None:
    network_name = _generation_network_name(project_id, deployment_id)
    observed = runner.run(
        ["docker", "network", "inspect", "--format", "{{json .Labels}}", network_name],
        timeout_seconds=30,
        check=False,
    )
    if observed.returncode != 0:
        return
    labels = json.loads(observed.stdout)
    assert labels.get("heimdall.managed") == "true"
    assert labels.get("heimdall.kind") == "routing-smoke-network"
    assert labels.get("heimdall.project-id") == str(project_id)
    assert labels.get("heimdall.deployment-id") == str(deployment_id)
    assert labels.get("heimdall.test-id") == TEST_ID
    runner.run(["docker", "network", "rm", network_name], timeout_seconds=30)


def _generation_network_name(project_id: UUID, deployment_id: UUID) -> str:
    return f"hm-routing-smoke-p{project_id.hex[:8]}-d{deployment_id.hex[:8]}"


def _assert_test_database_url(url: str) -> None:
    database_name = unquote(urlparse(url).path.removeprefix("/"))
    assert re.fullmatch(r"heimdall_routing_smoke_[a-z0-9_]+", database_name), (
        "public hostname smoke requires a dedicated heimdall_routing_smoke_* database"
    )


def _assert_empty_control_database(database: Database) -> None:
    with database.connection() as connection:
        row = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM projects) AS projects,
                (SELECT count(*) FROM project_public_routes) AS routes
            """
        ).fetchone()
    assert row == {"projects": 0, "routes": 0}, (
        "public hostname smoke requires an empty dedicated database"
    )


def _assert_test_edge_config_root(configured_value: str, test_id: str) -> Path:
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", test_id)
    configured_root = Path(configured_value)
    assert configured_root.is_absolute(), "test Edge config root must be absolute"
    assert not configured_root.is_symlink(), "test Edge config root must not be a symlink"
    config_root = configured_root.resolve(strict=True)
    root_metadata = config_root.stat()
    assert stat.S_ISDIR(root_metadata.st_mode), "test Edge config root must be a directory"
    assert root_metadata.st_uid == os.geteuid(), (
        "test Edge config root must be owned by the current user"
    )
    assert stat.S_IMODE(root_metadata.st_mode) == 0o700, "test Edge config root must have mode 0700"
    assert config_root.name == test_id, (
        "test Edge config root basename must exactly match HEIMDALL_TEST_ID"
    )

    marker = config_root / _EDGE_CONFIG_OWNER_MARKER
    assert marker.exists() or marker.is_symlink(), "test Edge config root requires an owner marker"
    _assert_private_regular_file(marker, "test Edge config owner marker")
    assert marker.read_text(encoding="utf-8") == f"{test_id}\n", (
        "test Edge config owner marker must contain the exact test id"
    )

    entries = {entry.name for entry in config_root.iterdir()}
    assert entries <= _EDGE_CONFIG_ALLOWED_ENTRIES, (
        "test Edge config root must contain only the owner marker and an optional "
        "no-route runtime snapshot"
    )
    lock_path = config_root / ".routing.lock"
    if lock_path.exists() or lock_path.is_symlink():
        _assert_private_regular_file(lock_path, "test Edge routing lock")
    current_path = config_root / "public-routes.conf"
    if current_path.exists() or current_path.is_symlink():
        _assert_private_regular_file(current_path, "test Edge route snapshot")
        assert current_path.read_text(encoding="utf-8") in _EDGE_CONFIG_NO_ROUTE_STATES, (
            "test Edge route snapshot must initially contain no public routes"
        )
    return config_root


def _assert_private_regular_file(path: Path, description: str) -> None:
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode), f"{description} must be a regular file"
    assert metadata.st_uid == os.geteuid(), f"{description} must be owned by the current user"
    assert stat.S_IMODE(metadata.st_mode) == 0o600, f"{description} must have mode 0600"


def _assert_test_edge_scope(
    runner: SubprocessCommandRunner,
    config_root: Path,
    *,
    edge_network: str,
    edge_container: str,
    test_id: str,
) -> None:
    assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", test_id)
    network = runner.run(
        ["docker", "network", "inspect", "--format", "{{json .Labels}}", edge_network],
        timeout_seconds=30,
    )
    network_labels = json.loads(network.stdout)
    assert network_labels.get("heimdall.managed") == "true"
    assert network_labels.get("heimdall.kind") == "edge-network"
    assert network_labels.get("heimdall.test-id") == test_id
    edge = runner.run(
        [
            "docker",
            "inspect",
            "--format",
            '{"labels":{{json .Config.Labels}},"running":{{json .State.Running}},'
            '"mounts":{{json .Mounts}}}',
            edge_container,
        ],
        timeout_seconds=30,
    )
    observation = json.loads(edge.stdout)
    labels = observation.get("labels")
    assert isinstance(labels, dict)
    assert labels.get("heimdall.managed") == "true"
    assert labels.get("heimdall.kind") == "edge-gateway"
    assert labels.get("heimdall.test-id") == test_id
    assert observation.get("running") is True
    mounts = observation.get("mounts")
    assert isinstance(mounts, list)
    route_mounts = [
        mount
        for mount in mounts
        if isinstance(mount, dict) and mount.get("Destination") == "/etc/nginx/routes"
    ]
    assert len(route_mounts) == 1, "test Edge must have exactly one /etc/nginx/routes mount"
    route_mount = route_mounts[0]
    assert route_mount.get("Type") == "bind", "test Edge routes mount must be a bind mount"
    assert route_mount.get("RW") is False, "test Edge routes bind mount must be read-only"
    source = route_mount.get("Source")
    assert isinstance(source, str) and source, "test Edge routes bind mount must have a source"
    source_path = Path(source)
    assert source_path.is_absolute(), "test Edge routes bind source must be absolute"
    assert source_path.resolve(strict=True) == config_root, (
        "test Edge routes bind source must exactly match the validated config root"
    )


class _EdgeScopeRunner:
    def __init__(self, config_root: Path, test_id: str) -> None:
        self.config_root = config_root
        self.test_id = test_id
        self.routes_mount_read_write = False

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        if list(arguments)[1:3] == ["network", "inspect"]:
            return CommandResult(
                0,
                json.dumps(
                    {
                        "heimdall.managed": "true",
                        "heimdall.kind": "edge-network",
                        "heimdall.test-id": self.test_id,
                    }
                ),
            )
        return CommandResult(
            0,
            json.dumps(
                {
                    "labels": {
                        "heimdall.managed": "true",
                        "heimdall.kind": "edge-gateway",
                        "heimdall.test-id": self.test_id,
                    },
                    "running": True,
                    "mounts": [
                        {
                            "Destination": "/etc/nginx/routes",
                            "Source": str(self.config_root),
                            "Type": "bind",
                            "RW": self.routes_mount_read_write,
                        }
                    ],
                }
            ),
        )


def test_edge_smoke_scope_requires_dedicated_no_route_root_and_exact_bind(
    tmp_path: Path,
) -> None:
    test_id = "public-hostname-routing-scope-test"
    config_root = tmp_path / test_id
    config_root.mkdir(mode=0o700)
    marker = config_root / _EDGE_CONFIG_OWNER_MARKER
    marker.write_text(f"{test_id}\n", encoding="utf-8")
    marker.chmod(0o600)

    validated_root = _assert_test_edge_config_root(str(config_root), test_id)
    runner = _EdgeScopeRunner(validated_root, test_id)
    _assert_test_edge_scope(
        runner,
        validated_root,
        edge_network="heimdall-edge-smoke",
        edge_container="heimdall-edge-gateway-smoke",
        test_id=test_id,
    )

    runner.config_root = tmp_path
    with pytest.raises(AssertionError, match="exactly match"):
        _assert_test_edge_scope(
            runner,
            validated_root,
            edge_network="heimdall-edge-smoke",
            edge_container="heimdall-edge-gateway-smoke",
            test_id=test_id,
        )
    runner.config_root = validated_root
    runner.routes_mount_read_write = True
    with pytest.raises(AssertionError, match="read-only"):
        _assert_test_edge_scope(
            runner,
            validated_root,
            edge_network="heimdall-edge-smoke",
            edge_container="heimdall-edge-gateway-smoke",
            test_id=test_id,
        )


def test_edge_smoke_scope_rejects_an_active_initial_snapshot(tmp_path: Path) -> None:
    test_id = "public-hostname-routing-active-test"
    config_root = tmp_path / test_id
    config_root.mkdir(mode=0o700)
    marker = config_root / _EDGE_CONFIG_OWNER_MARKER
    marker.write_text(f"{test_id}\n", encoding="utf-8")
    marker.chmod(0o600)
    snapshot = config_root / "public-routes.conf"
    snapshot.write_text("server { listen 80; }\n", encoding="utf-8")
    snapshot.chmod(0o600)

    with pytest.raises(AssertionError, match="initially contain no public routes"):
        _assert_test_edge_config_root(str(config_root), test_id)


def _delete_test_projects(url: str, project_ids: list[UUID]) -> None:
    if not project_ids:
        return
    database = Database(url)
    database.open()
    try:
        with database.connection() as connection:
            for project_id in project_ids:
                connection.execute("DELETE FROM projects WHERE id = %s", (project_id,))
        _assert_empty_control_database(database)
    finally:
        database.close()
