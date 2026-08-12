from __future__ import annotations

import json
from datetime import UTC, datetime

from test_runtime_models import runtime_deployment

from heimdall.deployments.diagnostics import (
    DIAGNOSTIC_ARTIFACT_MAX_BYTES,
    FailedCommandOutput,
)
from heimdall.deployments.worker import RuntimeFailure
from heimdall.runtime.deployment_diagnostics import DockerDeploymentDiagnosticCollector
from heimdall.runtime.logs import ServiceLogLine, ServiceLogSnapshot, ServiceLogStream
from heimdall.secrets.store import SecretStoreError


class DiagnosticSecrets:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def read(self, reference: str, fingerprint: str) -> str:
        try:
            return self.values[reference]
        except KeyError as error:
            raise SecretStoreError("missing") from error

    def create(self, reference_root, version, value=None):
        raise NotImplementedError

    def resolve(self, reference, fingerprint):
        raise NotImplementedError


class DiagnosticServiceLogs:
    def __init__(self, lines: tuple[ServiceLogLine, ...]) -> None:
        self.lines = lines

    def read(self, deployment, service_name):
        return ServiceLogSnapshot(
            deployment_id=deployment.id,
            services=(service_name,),
            service_name=service_name,
            retrieved_at=datetime.now(UTC),
            lines=self.lines,
            truncated=False,
        )

    def read_diagnostic(self, deployment, service_name):
        return self.read(deployment, service_name), "exited", 1


def _secret_values(deployment) -> dict[str, str]:
    service = deployment.config_snapshot["services"][0]
    database = deployment.config_snapshot["managedDatabase"]
    return {
        service["environment"][1]["secretReference"]: "project-secret-canary",
        database["credentialReference"]: "database-secret-canary",
    }


def test_collector_redacts_command_output_and_links_each_service() -> None:
    deployment = runtime_deployment()
    service_logs = DiagnosticServiceLogs(
        (
            ServiceLogLine(
                timestamp="2026-08-12T00:00:00Z",
                stream=ServiceLogStream.STDERR,
                message="already redacted service error",
            ),
        )
    )
    collector = DockerDeploymentDiagnosticCollector(
        service_logs,
        DiagnosticSecrets(_secret_values(deployment)),
    )
    failure = RuntimeFailure(
        "BUILD",
        "IMAGE_BUILD_FAILED",
        command_output=FailedCommandOutput(
            operation="DOCKER_BUILD",
            return_code=17,
            stdout="project-secret-canary compiling",
            stderr="database-secret-canary failed",
        ),
    )

    artifacts = collector.capture(deployment, failure)

    assert [item.kind for item in artifacts] == ["COMMAND_OUTPUT", "SERVICE_LOG"]
    command = artifacts[0]
    assert command.operation == "DOCKER_BUILD"
    assert command.return_code == 17
    text = "\n".join(line.message for line in command.lines)
    assert text.count("[REDACTED]") == 2
    assert "secret-canary" not in text
    assert artifacts[1].service_name == "api"
    assert artifacts[1].container_status == "exited"
    assert artifacts[1].container_exit_code == 1


def test_collector_fails_closed_when_command_redactions_cannot_be_prepared() -> None:
    deployment = runtime_deployment()
    collector = DockerDeploymentDiagnosticCollector(
        DiagnosticServiceLogs(()),
        DiagnosticSecrets({}),
    )

    artifacts = collector.capture(
        deployment,
        RuntimeFailure(
            "BUILD",
            "IMAGE_BUILD_FAILED",
            command_output=FailedCommandOutput(
                operation="DOCKER_BUILD",
                return_code=1,
                stdout="must not be persisted",
                stderr="",
            ),
        ),
    )

    command = artifacts[0]
    assert command.capture_status == "UNAVAILABLE"
    assert command.capture_code == "SERVICE_LOG_REDACTION_UNAVAILABLE"
    assert command.lines == ()


def test_collector_bounds_each_artifact_to_256_kib() -> None:
    deployment = runtime_deployment()
    collector = DockerDeploymentDiagnosticCollector(
        DiagnosticServiceLogs(()),
        DiagnosticSecrets(_secret_values(deployment)),
    )

    command = collector.capture(
        deployment,
        RuntimeFailure(
            "BUILD",
            "IMAGE_BUILD_FAILED",
            command_output=FailedCommandOutput(
                operation="DOCKER_BUILD",
                return_code=1,
                stdout="\n".join(f"line-{index}-{'x' * 16_000}" for index in range(100)),
                stderr="",
            ),
        ),
    )[0]

    encoded = json.dumps(
        [
            {
                "timestamp": line.timestamp,
                "stream": line.stream.value,
                "message": line.message,
            }
            for line in command.lines
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(encoded) <= DIAGNOSTIC_ARTIFACT_MAX_BYTES
    assert command.truncated is True
