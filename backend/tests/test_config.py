from pathlib import Path

from heimdall.config import Settings


def test_runtime_probe_and_broker_socket_defaults_preserve_host_execution(
    monkeypatch, tmp_path: Path
) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("HEIMDALL_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.delenv("HEIMDALL_RUNTIME_PROBE_HOST", raising=False)
    monkeypatch.delenv("HEIMDALL_BROKER_SOCKET_ROOT", raising=False)

    settings = Settings.from_environment()

    assert settings.runtime_probe_host == "127.0.0.1"
    assert settings.broker_socket_root == runtime_root.resolve()


def test_compose_runtime_probe_and_broker_socket_can_be_configured(
    monkeypatch, tmp_path: Path
) -> None:
    broker_root = tmp_path / "broker"
    monkeypatch.setenv("HEIMDALL_RUNTIME_PROBE_HOST", "host.docker.internal")
    monkeypatch.setenv("HEIMDALL_BROKER_SOCKET_ROOT", str(broker_root))

    settings = Settings.from_environment()

    assert settings.runtime_probe_host == "host.docker.internal"
    assert settings.broker_socket_root == broker_root.resolve()
