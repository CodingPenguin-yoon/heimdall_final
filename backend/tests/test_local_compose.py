from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[2]
BASE_COMPOSE = REPOSITORY_ROOT / "infra" / "dev" / "compose.yaml"
LINUX_COMPOSE = REPOSITORY_ROOT / "infra" / "dev" / "compose.linux.yaml"


def _compose_config(*files: Path) -> dict:
    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI is not installed")
    command = ["docker", "compose"]
    for file in files:
        command.extend(["--file", str(file)])
    command.extend(["config", "--format", "json"])
    environment = os.environ | {
        "HEIMDALL_CONTROL_DB_PASSWORD": "test-control-password",
        "HEIMDALL_MANAGED_DB_PROVISIONER_PASSWORD": "test-managed-password",
        "HEIMDALL_RUNTIME_ROOT": "/tmp/heimdall-runtime",
        "HEIMDALL_GIT_WORKSPACE_ROOT": "/tmp/heimdall-git",
        "HEIMDALL_MANAGEMENT_HOSTNAME": "heimdall.localhost",
        "HEIMDALL_DEPLOYMENT_BASE_DOMAIN": "deployments.localhost",
        "HEIMDALL_EDGE_CONFIG_ROOT": "/tmp/heimdall-edge",
        "HEIMDALL_AUTH_SECRET_ROOT": "/tmp/heimdall-auth",
    }
    try:
        result = subprocess.run(
            command,
            cwd=REPOSITORY_ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as error:
        pytest.fail(error.stderr)
    return json.loads(result.stdout)


def test_desktop_workers_keep_the_bridge_host_contract() -> None:
    services = _compose_config(BASE_COMPOSE)["services"]

    deployment_worker = services["worker"]
    routing_worker = services["routing-worker"]

    assert deployment_worker.get("network_mode") is None
    assert deployment_worker["environment"]["HEIMDALL_DATABASE_URL"].endswith(
        "@control-postgres:5432/heimdall_control"
    )
    assert deployment_worker["environment"]["HEIMDALL_RUNTIME_PROBE_HOST"] == (
        "host.docker.internal"
    )
    assert routing_worker.get("network_mode") is None
    assert routing_worker["environment"]["HEIMDALL_DATABASE_URL"].endswith(
        "@control-postgres:5432/heimdall_control"
    )
    assert routing_worker["environment"]["HEIMDALL_EDGE_PROBE_HOST"] == "host.docker.internal"


def test_linux_workers_use_the_merged_host_loopback_contract() -> None:
    services = _compose_config(BASE_COMPOSE, LINUX_COMPOSE)["services"]

    deployment_worker = services["worker"]
    routing_worker = services["routing-worker"]

    assert deployment_worker["network_mode"] == "host"
    assert deployment_worker.get("networks") is None
    assert deployment_worker.get("ports") is None
    assert deployment_worker["environment"]["HEIMDALL_DATABASE_URL"].endswith(
        "@127.0.0.1:55432/heimdall_control"
    )
    assert deployment_worker["environment"]["HEIMDALL_RUNTIME_PROBE_HOST"] == "127.0.0.1"
    assert routing_worker["network_mode"] == "host"
    assert routing_worker.get("networks") is None
    assert routing_worker.get("ports") is None
    assert routing_worker["environment"]["HEIMDALL_DATABASE_URL"].endswith(
        "@127.0.0.1:55432/heimdall_control"
    )
    assert routing_worker["environment"]["HEIMDALL_EDGE_PROBE_HOST"] == "127.0.0.1"
