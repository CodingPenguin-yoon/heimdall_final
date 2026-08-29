from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from heimdall.runtime.edge import (
    DockerEdgeConfigManager,
    EdgeFinalizeRejectedError,
    EdgeRouteChange,
    EdgeRouteProbe,
    EdgeRouteTarget,
    EdgeRuntimeError,
    render_edge_routes,
)
from heimdall.runtime.process import CommandExecutionError, CommandResult


class EdgeRunner:
    def __init__(
        self,
        *,
        config_valid: bool = True,
        reload_failures: int = 0,
        network_labels: dict[str, str] | None = None,
        edge_image: str = "nginx:1.29-alpine",
    ) -> None:
        self.config_valid = config_valid
        self.reload_failures = reload_failures
        self.network_labels = network_labels or {
            "heimdall.managed": "true",
            "heimdall.kind": "edge-network",
        }
        self.edge_image = edge_image
        self.calls: list[list[str]] = []
        self.reloads = 0

    def run(self, arguments, *, timeout_seconds, heartbeat=None, check=True) -> CommandResult:
        command = list(arguments)
        self.calls.append(command)
        if heartbeat is not None:
            heartbeat()
        if command[1:3] == ["network", "inspect"]:
            return CommandResult(0, json.dumps(self.network_labels))
        if command[1] == "inspect":
            return CommandResult(
                0,
                json.dumps(
                    {
                        "labels": {
                            "heimdall.managed": "true",
                            "heimdall.kind": "edge-gateway",
                        },
                        "image": self.edge_image,
                        "running": True,
                        "networks": {"heimdall-edge": {}},
                    }
                ),
            )
        if command[1:3] == ["run", "--rm"] and not self.config_valid:
            raise CommandExecutionError(CommandResult(1, "", "invalid config"))
        if command[1] == "exec":
            if command[3:5] == ["cat", "/etc/nginx/nginx.conf"]:
                return CommandResult(0, "events {}\nhttp { include /etc/nginx/routes/*.conf; }\n")
            if command[3:5] == [
                "cat",
                "/tmp/heimdall-edge-conf/management.conf",
            ]:
                return CommandResult(0, "# management\n")
            self.reloads += 1
            if self.reloads <= self.reload_failures:
                raise CommandExecutionError(CommandResult(1, "", "reload failed"))
        return CommandResult(0, "")


class RecordingProbe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def management(self, hostname, heartbeat) -> None:
        heartbeat()
        self.calls.append(("management", hostname))

    def routed(self, hostname, heartbeat) -> None:
        heartbeat()
        self.calls.append(("routed", hostname))

    def not_found(self, hostname, heartbeat) -> None:
        heartbeat()
        self.calls.append(("not-found", hostname))


def route(hostname: str = "beta.deployments.test") -> EdgeRouteTarget:
    project_id = uuid4()
    return EdgeRouteTarget(
        project_id=project_id,
        hostname=hostname,
        gateway_alias=f"hm-p{project_id.hex[:12]}-gateway",
    )


def manager(tmp_path: Path, runner: EdgeRunner, probe: RecordingProbe) -> DockerEdgeConfigManager:
    return DockerEdgeConfigManager(
        runner,
        probe,
        tmp_path / "edge",
        "control.management.test",
    )


def test_edge_config_is_deterministic_and_only_targets_project_gateways() -> None:
    beta = route()
    alpha = route("alpha.deployments.test")

    rendered = render_edge_routes([beta, alpha])

    assert rendered.index(alpha.hostname) < rendered.index(beta.hostname)
    assert alpha.gateway_alias in rendered
    assert beta.gateway_alias in rendered
    assert "resolve;" in rendered
    assert "proxy_set_header Host $host;" in rendered
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in rendered
    assert "-g-" not in rendered


def test_invalid_candidate_does_not_replace_or_reload_current_config(tmp_path: Path) -> None:
    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner(config_valid=False)
    probe = RecordingProbe()

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, probe).apply(
            [route()],
            EdgeRouteChange("beta.deployments.test", True),
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: None,
        )

    assert raised.value.code == "EDGE_CONFIG_INVALID"
    assert current.read_text(encoding="utf-8") == "# previous\n"
    assert runner.reloads == 0
    assert probe.calls == []


def test_candidate_validation_uses_running_edge_main_and_management_config(
    tmp_path: Path,
) -> None:
    runner = EdgeRunner()
    probe = RecordingProbe()

    manager(tmp_path, runner, probe).apply(
        [route("student-a.deployments.routing-smoke.test")],
        EdgeRouteChange("student-a.deployments.routing-smoke.test", True),
        heartbeat=lambda: None,
        fence=lambda: None,
        finalize=lambda: None,
    )

    assert [
        "docker",
        "exec",
        "heimdall-edge-gateway",
        "cat",
        "/etc/nginx/nginx.conf",
    ] in runner.calls
    assert [
        "docker",
        "exec",
        "heimdall-edge-gateway",
        "cat",
        "/tmp/heimdall-edge-conf/management.conf",
    ] in runner.calls
    validation = next(call for call in runner.calls if call[1:3] == ["run", "--rm"])
    mounts = [validation[index + 1] for index, value in enumerate(validation) if value == "--mount"]
    assert any("dst=/etc/nginx/nginx.conf" in mount for mount in mounts)
    assert any("dst=/tmp/heimdall-edge-conf/management.conf" in mount for mount in mounts)
    assert any("dst=/etc/nginx/routes/public-routes.conf" in mount for mount in mounts)
    assert not (tmp_path / "edge" / ".routing-transaction.json").exists()


def test_reload_failure_restores_previous_config_and_confirms_old_route(tmp_path: Path) -> None:
    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner(reload_failures=1)
    probe = RecordingProbe()

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, probe).apply(
            [route()],
            EdgeRouteChange(
                "beta.deployments.test",
                True,
                previous_hostname="alpha.deployments.test",
            ),
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: None,
        )

    assert raised.value.code == "EDGE_RELOAD_FAILED"
    assert current.read_text(encoding="utf-8") == "# previous\n"
    assert runner.reloads == 2
    assert probe.calls == [
        ("management", "control.management.test"),
        ("routed", "alpha.deployments.test"),
        ("not-found", "beta.deployments.test"),
    ]
    assert not (tmp_path / "edge" / ".routing-transaction.json").exists()


def test_rejected_finalize_after_probe_restores_previous_config(tmp_path: Path) -> None:
    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner()
    probe = RecordingProbe()

    with pytest.raises(EdgeFinalizeRejectedError):
        manager(tmp_path, runner, probe).apply(
            [route()],
            EdgeRouteChange(
                "beta.deployments.test",
                True,
                previous_hostname="alpha.deployments.test",
            ),
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: (_ for _ in ()).throw(EdgeFinalizeRejectedError()),
        )

    assert current.read_text(encoding="utf-8") == "# previous\n"
    assert runner.reloads == 2
    assert probe.calls[-3:] == [
        ("management", "control.management.test"),
        ("routed", "alpha.deployments.test"),
        ("not-found", "beta.deployments.test"),
    ]


def test_unknown_finalize_failure_preserves_candidate_until_canonical_reconciliation(
    tmp_path: Path,
) -> None:
    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    target = route()
    previous_target = EdgeRouteTarget(
        target.project_id,
        "alpha.deployments.test",
        target.gateway_alias,
    )
    previous = render_edge_routes([previous_target])
    current.write_text(previous, encoding="utf-8")
    runner = EdgeRunner()
    probe = RecordingProbe()

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, probe).apply(
            [target],
            EdgeRouteChange(
                target.hostname,
                True,
                previous_hostname=previous_target.hostname,
            ),
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: (_ for _ in ()).throw(RuntimeError("commit outcome unknown")),
        )

    journal = tmp_path / "edge" / ".routing-transaction.json"
    assert raised.value.code == "EDGE_FINALIZE_UNCERTAIN"
    assert raised.value.uncertain is True
    assert current.read_text(encoding="utf-8") == render_edge_routes([target])
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "PREPARED"
    assert runner.reloads == 1

    manager(tmp_path, runner, probe).apply(
        [previous_target],
        None,
        heartbeat=lambda: None,
        fence=lambda: None,
        finalize=lambda: None,
        probe_all_routes=True,
    )

    assert current.read_text(encoding="utf-8") == previous
    assert not journal.exists()


def test_fence_lost_after_reload_restores_previous_config(tmp_path: Path) -> None:
    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner()
    probe = RecordingProbe()
    fences = 0

    def fence() -> None:
        nonlocal fences
        fences += 1
        if fences == 3:
            raise LookupError("stale revision")

    with pytest.raises(LookupError):
        manager(tmp_path, runner, probe).apply(
            [route()],
            EdgeRouteChange("beta.deployments.test", True),
            heartbeat=lambda: None,
            fence=fence,
            finalize=lambda: None,
        )

    assert current.read_text(encoding="utf-8") == "# previous\n"
    assert runner.reloads == 2
    assert probe.calls[-2:] == [
        ("management", "control.management.test"),
        ("not-found", "beta.deployments.test"),
    ]


def test_db_free_startup_keeps_pre_finalize_candidate_until_canonical_reconciliation(
    tmp_path: Path,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    target = route()
    previous_target = EdgeRouteTarget(
        target.project_id,
        "alpha.deployments.test",
        target.gateway_alias,
    )
    previous = render_edge_routes([previous_target])
    current.write_text(previous, encoding="utf-8")
    runner = EdgeRunner()
    first_probe = RecordingProbe()
    fences = 0

    def crash_after_reload() -> None:
        nonlocal fences
        fences += 1
        if fences == 3:
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        manager(tmp_path, runner, first_probe).apply(
            [target],
            EdgeRouteChange(
                target.hostname,
                True,
                previous_hostname="alpha.deployments.test",
            ),
            heartbeat=lambda: None,
            fence=crash_after_reload,
            finalize=lambda: None,
        )

    journal = tmp_path / "edge" / ".routing-transaction.json"
    assert current.read_text(encoding="utf-8") == render_edge_routes([target])
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "PREPARED"

    recovery_probe = RecordingProbe()
    recovered = manager(tmp_path, runner, recovery_probe).recover_interrupted()

    assert recovered is True
    assert current.read_text(encoding="utf-8") == render_edge_routes([target])
    assert journal.exists()
    assert runner.reloads == 1
    assert recovery_probe.calls == []

    manager(tmp_path, runner, recovery_probe).apply(
        [previous_target],
        None,
        heartbeat=lambda: None,
        fence=lambda: None,
        finalize=lambda: None,
        probe_all_routes=True,
    )

    assert current.read_text(encoding="utf-8") == previous
    assert not journal.exists()
    assert runner.reloads == 2
    assert recovery_probe.calls == [
        ("management", "control.management.test"),
        ("routed", "alpha.deployments.test"),
    ]


def test_db_canonical_snapshot_preserves_candidate_after_finalize_crash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner()
    target = route()
    edge = manager(tmp_path, runner, RecordingProbe())
    finalized = False

    def finalize() -> None:
        nonlocal finalized
        finalized = True

    def crash_before_commit_marker(transaction) -> None:
        raise SimulatedProcessCrash

    monkeypatch.setattr(edge, "_mark_transaction_committed", crash_before_commit_marker)

    with pytest.raises(SimulatedProcessCrash):
        edge.apply(
            [target],
            EdgeRouteChange(target.hostname, True),
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=finalize,
        )

    journal = tmp_path / "edge" / ".routing-transaction.json"
    candidate = render_edge_routes([target])
    assert finalized is True
    assert current.read_text(encoding="utf-8") == candidate
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "PREPARED"

    recovery_probe = RecordingProbe()
    recovery = manager(tmp_path, runner, recovery_probe)
    assert recovery.recover_interrupted() is True
    assert current.read_text(encoding="utf-8") == candidate
    assert journal.exists()

    recovery.apply(
        [target],
        None,
        heartbeat=lambda: None,
        fence=lambda: None,
        finalize=lambda: None,
        probe_all_routes=True,
    )

    assert current.read_text(encoding="utf-8") == candidate
    assert not journal.exists()
    assert recovery_probe.calls == [
        ("management", "control.management.test"),
        ("routed", target.hostname),
    ]


def test_committed_journal_clear_failure_recovers_without_rolling_back_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner()
    target = route()
    journal = tmp_path / "edge" / ".routing-transaction.json"
    original_unlink = Path.unlink
    failed = False

    def fail_journal_unlink_once(path: Path, missing_ok: bool = False) -> None:
        nonlocal failed
        if path == journal and not failed:
            failed = True
            raise OSError("simulated journal clear failure")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_journal_unlink_once)

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, RecordingProbe()).apply(
            [target],
            EdgeRouteChange(target.hostname, True),
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: None,
        )

    candidate = render_edge_routes([target])
    assert raised.value.code == "EDGE_TRANSACTION_JOURNAL_CLEAR_FAILED"
    assert raised.value.uncertain is True
    assert current.read_text(encoding="utf-8") == candidate
    assert json.loads(journal.read_text(encoding="utf-8"))["phase"] == "COMMITTED"

    recovery_probe = RecordingProbe()
    recovery = manager(tmp_path, runner, recovery_probe)
    assert recovery.recover_interrupted() is True
    assert current.read_text(encoding="utf-8") == candidate
    assert journal.exists()

    recovery.apply(
        [target],
        None,
        heartbeat=lambda: None,
        fence=lambda: None,
        finalize=lambda: None,
        probe_all_routes=True,
    )

    assert current.read_text(encoding="utf-8") == candidate
    assert not journal.exists()


def test_interrupted_recovery_refuses_an_unrelated_current_config(tmp_path: Path) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    current = tmp_path / "edge" / "public-routes.conf"
    current.parent.mkdir()
    current.write_text("# previous\n", encoding="utf-8")
    runner = EdgeRunner()
    target = route()
    fences = 0

    def crash_after_reload() -> None:
        nonlocal fences
        fences += 1
        if fences == 3:
            raise SimulatedProcessCrash

    with pytest.raises(SimulatedProcessCrash):
        manager(tmp_path, runner, RecordingProbe()).apply(
            [target],
            EdgeRouteChange(
                target.hostname,
                True,
                previous_hostname="alpha.deployments.test",
            ),
            heartbeat=lambda: None,
            fence=crash_after_reload,
            finalize=lambda: None,
        )
    current.write_text("# unrelated operator config\n", encoding="utf-8")

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, RecordingProbe()).recover_interrupted()

    assert raised.value.code == "EDGE_CONFIG_CHANGED_DURING_RECOVERY"
    assert raised.value.uncertain is True
    assert current.read_text(encoding="utf-8") == "# unrelated operator config\n"
    assert (tmp_path / "edge" / ".routing-transaction.json").exists()
    assert runner.reloads == 1


def test_unmanaged_edge_network_blocks_all_mutation(tmp_path: Path) -> None:
    runner = EdgeRunner(network_labels={"heimdall.managed": "false"})
    probe = RecordingProbe()

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, probe).apply(
            [route()],
            None,
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: None,
        )

    assert raised.value.code == "EDGE_NETWORK_CONFLICT"
    assert not any(call[1] in {"run", "exec"} for call in runner.calls)
    assert not (tmp_path / "edge" / "public-routes.conf").exists()


def test_unexpected_edge_image_blocks_config_mutation(tmp_path: Path) -> None:
    runner = EdgeRunner(edge_image="nginx:latest")
    probe = RecordingProbe()

    with pytest.raises(EdgeRuntimeError) as raised:
        manager(tmp_path, runner, probe).apply(
            [route()],
            None,
            heartbeat=lambda: None,
            fence=lambda: None,
            finalize=lambda: None,
        )

    assert raised.value.code == "EDGE_GATEWAY_CONFLICT"
    assert not any(call[1] in {"run", "exec"} for call in runner.calls)


def test_route_probe_does_not_accept_default_404_as_an_active_route(monkeypatch) -> None:
    class Response:
        status = 404

        @staticmethod
        def getheader(name):
            return None

        @staticmethod
        def close() -> None:
            return None

    class Connection:
        def __init__(self, host, port, timeout) -> None:
            return None

        def request(self, method, path, headers) -> None:
            return None

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr("heimdall.runtime.edge.HTTPConnection", Connection)
    probe = EdgeRouteProbe("127.0.0.1", 8088, timeout_seconds=0.001, interval_seconds=0)

    with pytest.raises(EdgeRuntimeError) as raised:
        probe.routed("missing.deployments.test", lambda: None)

    assert raised.value.code == "EDGE_ROUTE_PROBE_FAILED"
    probe.not_found("missing.deployments.test", lambda: None)


def test_management_probe_requires_the_exact_edge_route_marker(monkeypatch) -> None:
    class Response:
        status = 200
        management_marker: str | None = None

        @classmethod
        def getheader(cls, name):
            if name == "X-Heimdall-Management":
                return cls.management_marker
            return None

        @staticmethod
        def close() -> None:
            return None

    class Connection:
        def __init__(self, host, port, timeout) -> None:
            return None

        def request(self, method, path, headers) -> None:
            return None

        @staticmethod
        def getresponse():
            return Response()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr("heimdall.runtime.edge.HTTPConnection", Connection)
    probe = EdgeRouteProbe("127.0.0.1", 8088, timeout_seconds=0.001, interval_seconds=0)

    with pytest.raises(EdgeRuntimeError) as raised:
        probe.management("control.management.test", lambda: None)

    assert raised.value.code == "EDGE_MANAGEMENT_PROBE_FAILED"
    Response.management_marker = "true"
    probe.management("control.management.test", lambda: None)
