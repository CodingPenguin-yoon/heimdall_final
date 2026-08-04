from pathlib import Path

import pytest

from heimdall.secrets.store import FileSecretStore, SecretStoreError


def test_file_secret_store_writes_owner_only_version(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "runtime")

    metadata = store.create("projects/p1/environment/api/jwt_secret", 1, "canary")
    secret_path = tmp_path / "runtime" / metadata.reference

    assert secret_path.read_text() == "canary"
    assert secret_path.stat().st_mode & 0o777 == 0o400
    assert store.read(metadata.reference, metadata.fingerprint) == "canary"
    assert store.resolve(metadata.reference, metadata.fingerprint) == secret_path


def test_file_secret_store_rejects_path_escape(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "runtime")

    with pytest.raises(SecretStoreError, match="canonical relative"):
        store.create("../outside", 1, "canary")
