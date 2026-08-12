from __future__ import annotations

import tomllib
from pathlib import Path


def test_backend_wheel_includes_database_migrations() -> None:
    backend_root = Path(__file__).parents[1]
    configuration = tomllib.loads((backend_root / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = configuration["tool"]["setuptools"].get("package-data", {})

    assert "migrations/*.sql" in package_data.get("heimdall", [])
