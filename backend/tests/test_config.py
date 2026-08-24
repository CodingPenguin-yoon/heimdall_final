from pathlib import Path

import pytest

from heimdall.config import Settings

SHARED_HOST_ROOTS = (
    "HEIMDALL_RUNTIME_ROOT",
    "HEIMDALL_GIT_WORKSPACE_ROOT",
    "HEIMDALL_EDGE_CONFIG_ROOT",
)


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
    assert settings.project_database_runtime_host == "host.docker.internal"
    assert settings.project_database_runtime_port == 55433
    assert settings.management_hostname == "heimdall.localhost"
    assert settings.deployment_base_domain == "deployments.localhost"
    assert settings.edge_network_name == "heimdall-edge"
    assert settings.auth_secret_root == Path("/run/secrets/heimdall/auth")
    assert settings.auth_secret_source_root == settings.auth_secret_root


def test_compose_runtime_probe_and_broker_socket_can_be_configured(
    monkeypatch, tmp_path: Path
) -> None:
    broker_root = tmp_path / "broker"
    monkeypatch.setenv("HEIMDALL_RUNTIME_PROBE_HOST", "host.docker.internal")
    monkeypatch.setenv("HEIMDALL_BROKER_SOCKET_ROOT", str(broker_root))

    settings = Settings.from_environment()

    assert settings.runtime_probe_host == "host.docker.internal"
    assert settings.broker_socket_root == broker_root.resolve()


def test_auth_secret_root_stays_absolute_without_resolving_final_symlink(
    monkeypatch, tmp_path: Path
) -> None:
    real_root = tmp_path / "real-auth"
    real_root.mkdir()
    linked_root = tmp_path / "linked-auth"
    linked_root.symlink_to(real_root, target_is_directory=True)
    monkeypatch.setenv("HEIMDALL_AUTH_SECRET_ROOT", str(linked_root))

    settings = Settings.from_environment()

    assert settings.auth_secret_root == linked_root
    assert settings.auth_secret_root.is_symlink()
    assert settings.auth_secret_source_root == linked_root
    assert settings.auth_secret_source_root.is_symlink()


def test_auth_secret_root_must_be_absolute(monkeypatch) -> None:
    monkeypatch.setenv("HEIMDALL_AUTH_SECRET_ROOT", "relative/auth")

    try:
        Settings.from_environment()
    except ValueError as error:
        assert "absolute path" in str(error)
    else:
        raise AssertionError("relative authentication secret root must be rejected")


def test_auth_secret_source_root_must_be_absolute(monkeypatch) -> None:
    monkeypatch.setenv("HEIMDALL_AUTH_SECRET_SOURCE_ROOT", "relative/auth")

    with pytest.raises(ValueError, match=r"HEIMDALL_AUTH_SECRET_SOURCE_ROOT.*absolute"):
        Settings.from_environment()


@pytest.mark.parametrize("shared_root_env", SHARED_HOST_ROOTS)
@pytest.mark.parametrize(
    "relationship",
    ("equal", "auth-inside-shared", "shared-inside-auth"),
)
def test_auth_secret_source_root_cannot_overlap_shared_host_roots(
    monkeypatch,
    tmp_path: Path,
    shared_root_env: str,
    relationship: str,
) -> None:
    shared_root = tmp_path / "shared"
    auth_source_root = tmp_path / "auth"
    if relationship == "equal":
        auth_source_root = tmp_path / "unused" / ".." / "shared"
    elif relationship == "auth-inside-shared":
        auth_source_root = tmp_path / "unused" / ".." / "shared" / "auth"
    else:
        shared_root = auth_source_root / "shared"
    monkeypatch.setenv(shared_root_env, str(shared_root))
    monkeypatch.setenv("HEIMDALL_AUTH_SECRET_SOURCE_ROOT", str(auth_source_root))

    with pytest.raises(
        ValueError,
        match=rf"HEIMDALL_AUTH_SECRET_SOURCE_ROOT must not overlap {shared_root_env}",
    ):
        Settings.from_environment()


@pytest.mark.parametrize("shared_root_env", SHARED_HOST_ROOTS)
def test_auth_secret_source_root_allows_lexically_normalized_siblings(
    monkeypatch,
    tmp_path: Path,
    shared_root_env: str,
) -> None:
    shared_root = tmp_path / "shared"
    auth_source_root = tmp_path / "auth" / ".." / "shared-sibling"
    monkeypatch.setenv(shared_root_env, str(shared_root))
    monkeypatch.setenv("HEIMDALL_AUTH_SECRET_SOURCE_ROOT", str(auth_source_root))

    settings = Settings.from_environment()

    assert settings.auth_secret_source_root == tmp_path / "shared-sibling"


def test_public_hostname_settings_are_normalized_and_reserved_labels_are_bounded(
    monkeypatch,
) -> None:
    monkeypatch.setenv("HEIMDALL_MANAGEMENT_HOSTNAME", "Control.Example.Test.")
    monkeypatch.setenv("HEIMDALL_DEPLOYMENT_BASE_DOMAIN", "Preview.Example.Test.")
    monkeypatch.setenv("HEIMDALL_RESERVED_PUBLIC_SUBDOMAINS", "Docs, status")

    settings = Settings.from_environment()

    assert settings.management_hostname == "control.example.test"
    assert settings.deployment_base_domain == "preview.example.test"
    assert settings.reserved_public_subdomains == (
        "admin",
        "api",
        "control",
        "docs",
        "status",
        "www",
    )


def test_management_hostname_cannot_be_inside_the_deployment_domain(monkeypatch) -> None:
    monkeypatch.setenv("HEIMDALL_MANAGEMENT_HOSTNAME", "control.preview.example.test")
    monkeypatch.setenv("HEIMDALL_DEPLOYMENT_BASE_DOMAIN", "preview.example.test")

    try:
        Settings.from_environment()
    except ValueError as error:
        assert "outside the deployment base domain" in str(error)
    else:
        raise AssertionError("nested management hostname must be rejected")
