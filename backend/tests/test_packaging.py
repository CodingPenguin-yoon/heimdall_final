from __future__ import annotations

import tomllib
from pathlib import Path


def test_backend_wheel_includes_database_migrations() -> None:
    backend_root = Path(__file__).parents[1]
    configuration = tomllib.loads((backend_root / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = configuration["tool"]["setuptools"].get("package-data", {})

    assert "migrations/*.sql" in package_data.get("heimdall", [])


def test_backend_declares_auth_dependencies_and_initializer_entrypoint() -> None:
    backend_root = Path(__file__).parents[1]
    configuration = tomllib.loads((backend_root / "pyproject.toml").read_text(encoding="utf-8"))

    dependencies = configuration["project"]["dependencies"]
    scripts = configuration["project"]["scripts"]

    assert any(item.startswith("argon2-cffi") for item in dependencies)
    assert any(item.startswith("itsdangerous") for item in dependencies)
    assert scripts["heimdall-admin-init"] == "heimdall.auth.cli:main"


def test_backend_container_uses_the_auth_validating_app_factory() -> None:
    backend_root = Path(__file__).parents[1]
    dockerfile = (backend_root / "Dockerfile").read_text(encoding="utf-8")

    assert (
        'CMD ["uvicorn", "--factory", "heimdall.main:create_app", '
        '"--host", "0.0.0.0", "--port", "8000"]'
    ) in dockerfile
