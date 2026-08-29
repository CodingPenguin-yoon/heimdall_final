from pathlib import Path
from uuid import uuid4

import pytest

import heimdall.secrets.store as secret_store_module
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


def test_project_delete_removes_only_exact_uuid_subtree_and_keeps_lock_outside_it(
    tmp_path: Path,
) -> None:
    store = FileSecretStore(tmp_path / "runtime")
    deleted_project = uuid4()
    kept_project = uuid4()
    deleted = store.create(f"projects/{deleted_project}/environment/api/token", 1, "delete-me")
    kept = store.create(f"projects/{kept_project}/environment/api/token", 1, "keep-me")

    with store.project_operation_lock(deleted_project):
        store.delete_project_subtree(deleted_project)

    assert store.project_subtree_absent(deleted_project) is True
    assert store.project_subtree_absent(kept_project) is False
    assert not (tmp_path / "runtime" / deleted.reference).exists()
    assert (tmp_path / "runtime" / kept.reference).read_text() == "keep-me"
    lock = tmp_path / "runtime" / ".locks" / "projects" / f"{deleted_project}.lock"
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o600


def test_project_delete_preserves_subtree_when_a_descendant_is_a_symlink(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "runtime")
    project_id = uuid4()
    secret = store.create(f"projects/{project_id}/environment/api/token", 1, "keep-me")
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (tmp_path / "runtime" / "projects" / str(project_id) / "unsafe").symlink_to(outside)

    with (
        store.project_operation_lock(project_id),
        pytest.raises(SecretStoreError, match="unsafe project secret subtree"),
    ):
        store.delete_project_subtree(project_id)

    assert (tmp_path / "runtime" / secret.reference).read_text() == "keep-me"
    assert outside.read_text() == "outside"


def test_project_delete_preserves_subtree_with_broad_directory_permissions(
    tmp_path: Path,
) -> None:
    store = FileSecretStore(tmp_path / "runtime")
    project_id = uuid4()
    secret = store.create(f"projects/{project_id}/environment/api/token", 1, "keep-me")
    project_root = tmp_path / "runtime" / "projects" / str(project_id)
    project_root.chmod(0o755)

    with (
        store.project_operation_lock(project_id),
        pytest.raises(SecretStoreError, match="unsafe project secret subtree"),
    ):
        store.delete_project_subtree(project_id)

    assert (tmp_path / "runtime" / secret.reference).read_text() == "keep-me"
    assert project_root.stat().st_mode & 0o777 == 0o755


def test_nonblocking_project_operation_lock_reports_an_active_writer(tmp_path: Path) -> None:
    store = FileSecretStore(tmp_path / "runtime")
    project_id = uuid4()

    with (
        store.project_operation_lock(project_id),
        pytest.raises(SecretStoreError, match="operation is active"),
        store.project_operation_lock(project_id, blocking=False),
    ):
        pass


def test_project_delete_rejects_target_inode_replacement_before_rmdir(
    tmp_path: Path, monkeypatch
) -> None:
    store = FileSecretStore(tmp_path / "runtime")
    project_id = uuid4()
    store.create(f"projects/{project_id}/environment/api/token", 1, "keep-identity")
    target = tmp_path / "runtime" / "projects" / str(project_id)
    original_delete_tree = secret_store_module._delete_tree
    original_stat = secret_store_module.os.stat
    deleted_contents = False

    def finish_delete(descriptor: int) -> None:
        nonlocal deleted_contents
        original_delete_tree(descriptor)
        deleted_contents = True

    def replaced_stat(path, *args, **kwargs):
        metadata = original_stat(path, *args, **kwargs)
        if deleted_contents and path == str(project_id) and kwargs.get("dir_fd") is not None:
            values = list(metadata)
            values[1] += 1
            return secret_store_module.os.stat_result(values)
        return metadata

    monkeypatch.setattr(secret_store_module, "_delete_tree", finish_delete)
    monkeypatch.setattr(secret_store_module.os, "stat", replaced_stat)

    with (
        store.project_operation_lock(project_id),
        pytest.raises(SecretStoreError, match="identity changed"),
    ):
        store.delete_project_subtree(project_id)

    assert target.is_dir()


def test_project_delete_rechecks_secret_file_inode_before_unlink(
    tmp_path: Path, monkeypatch
) -> None:
    store = FileSecretStore(tmp_path / "runtime")
    project_id = uuid4()
    stored = store.create(f"projects/{project_id}/environment/api/token", 1, "keep")
    original_stat = secret_store_module.os.stat
    relevant_calls = 0

    def replaced_stat(path, *args, **kwargs):
        nonlocal relevant_calls
        metadata = original_stat(path, *args, **kwargs)
        if path == "v1.secret" and kwargs.get("dir_fd") is not None:
            relevant_calls += 1
            if relevant_calls >= 3:
                values = list(metadata)
                values[1] += 1
                return secret_store_module.os.stat_result(values)
        return metadata

    monkeypatch.setattr(secret_store_module.os, "stat", replaced_stat)

    with (
        store.project_operation_lock(project_id),
        pytest.raises(SecretStoreError, match="identity changed"),
    ):
        store.delete_project_subtree(project_id)

    assert (tmp_path / "runtime" / stored.reference).is_file()
