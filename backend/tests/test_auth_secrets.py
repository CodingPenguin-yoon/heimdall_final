from __future__ import annotations

import hashlib
import hmac
import stat
from pathlib import Path

import pytest
from argon2 import PasswordHasher, Type

from heimdall.auth import cli
from heimdall.auth import secrets as auth_secrets
from heimdall.auth.secrets import (
    PASSWORD_HASH_FILENAME,
    SIGNING_KEY_FILENAME,
    AuthSecretError,
    initialize_admin_secrets,
    load_admin_secrets,
)


def test_initializer_creates_loadable_owner_only_argon2id_secrets(tmp_path: Path) -> None:
    root = tmp_path / "auth"

    initialize_admin_secrets(root, "correct horse battery staple", "correct horse battery staple")
    loaded = load_admin_secrets(root)

    hash_path = root / PASSWORD_HASH_FILENAME
    key_path = root / SIGNING_KEY_FILENAME
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(hash_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    is_argon2id = loaded.password_hash.startswith("$argon2id$")
    assert is_argon2id is True
    assert PasswordHasher().verify(loaded.password_hash, "correct horse battery staple") is True
    key_matches = hmac.compare_digest(
        loaded.signing_key,
        key_path.read_text(encoding="utf-8"),
    )
    assert key_matches is True
    assert loaded.credential_revision == hashlib.sha256(hash_path.read_bytes()).hexdigest()
    password_exposed = "correct horse battery staple" in hash_path.read_text(encoding="utf-8")
    assert password_exposed is False


def test_initializer_rejects_symlink_ancestor_before_creating_secret_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    root = linked_parent / "auth"
    temporary_directory_called = False

    def unexpected_temporary_directory(*args: object, **kwargs: object) -> str:
        nonlocal temporary_directory_called
        temporary_directory_called = True
        raise AssertionError("temporary secret directory must not be created")

    monkeypatch.setattr(auth_secrets.tempfile, "mkdtemp", unexpected_temporary_directory)

    with pytest.raises(AuthSecretError, match="symlink"):
        initialize_admin_secrets(root, "ancestor-canary", "ancestor-canary")

    assert temporary_directory_called is False
    assert list(real_parent.iterdir()) == []
    assert list(tmp_path.rglob(PASSWORD_HASH_FILENAME)) == []
    assert list(tmp_path.rglob(SIGNING_KEY_FILENAME)) == []


@pytest.mark.parametrize("git_marker_kind", ("directory", "file", "symlink"))
def test_initializer_rejects_git_worktree_before_creating_secret_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    git_marker_kind: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    git_marker = repository / ".git"
    if git_marker_kind == "directory":
        git_marker.mkdir()
    elif git_marker_kind == "file":
        git_marker.write_text("gitdir: ../metadata", encoding="utf-8")
    else:
        git_metadata = tmp_path / "git-metadata"
        git_metadata.mkdir()
        git_marker.symlink_to(git_metadata, target_is_directory=True)
    root = repository / "auth"
    temporary_directory_called = False

    def unexpected_temporary_directory(*args: object, **kwargs: object) -> str:
        nonlocal temporary_directory_called
        temporary_directory_called = True
        raise AssertionError("temporary secret directory must not be created")

    monkeypatch.setattr(auth_secrets.tempfile, "mkdtemp", unexpected_temporary_directory)

    with pytest.raises(AuthSecretError, match="outside a Git worktree"):
        initialize_admin_secrets(root, "git-canary", "git-canary")

    assert temporary_directory_called is False
    assert root.exists() is False
    assert list(repository.glob(".auth-*")) == []
    assert list(tmp_path.rglob(PASSWORD_HASH_FILENAME)) == []
    assert list(tmp_path.rglob(SIGNING_KEY_FILENAME)) == []


def test_initializer_accepts_canonical_path_outside_git_worktree_with_missing_parents(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve() / "canonical" / "nested" / "auth"

    initialize_admin_secrets(root, "canonical-canary", "canonical-canary")

    loaded = load_admin_secrets(root)
    assert PasswordHasher().verify(loaded.password_hash, "canonical-canary") is True
    assert {path.name for path in root.iterdir()} == {
        PASSWORD_HASH_FILENAME,
        SIGNING_KEY_FILENAME,
    }


def test_initializer_rejects_mismatch_without_creating_target(tmp_path: Path) -> None:
    root = tmp_path / "auth"

    with pytest.raises(AuthSecretError, match="confirmation"):
        initialize_admin_secrets(root, "first-canary", "second-canary")

    assert root.exists() is False


def test_initializer_never_overwrites_an_existing_secret_pair(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "original-canary", "original-canary")
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / PASSWORD_HASH_FILENAME, root / SIGNING_KEY_FILENAME)
    }

    with pytest.raises(AuthSecretError, match="already exists"):
        initialize_admin_secrets(root, "replacement-canary", "replacement-canary")

    assert {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (root / PASSWORD_HASH_FILENAME, root / SIGNING_KEY_FILENAME)
    } == before


def test_initializer_cleans_up_both_files_when_pair_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    root = tmp_path / "auth"
    original_write = auth_secrets._write_new_private_file
    writes = 0

    def fail_second_write(path: Path, payload: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected pair write failure")
        original_write(path, payload)

    monkeypatch.setattr(auth_secrets, "_write_new_private_file", fail_second_write)

    with pytest.raises(OSError, match="injected"):
        initialize_admin_secrets(root, "pair-canary", "pair-canary")

    assert root.exists() is False
    assert list(tmp_path.glob(".auth-*")) == []


def test_loader_rejects_symlink_root_without_resolving_it(tmp_path: Path) -> None:
    real_root = tmp_path / "real-auth"
    initialize_admin_secrets(real_root, "symlink-canary", "symlink-canary")
    linked_root = tmp_path / "linked-auth"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(AuthSecretError, match="symlink"):
        load_admin_secrets(linked_root)


@pytest.mark.parametrize(
    ("target", "mode", "message"),
    [
        ("root", 0o750, "0700"),
        (PASSWORD_HASH_FILENAME, 0o640, "0600"),
        (SIGNING_KEY_FILENAME, 0o644, "0600"),
    ],
)
def test_loader_rejects_broad_permissions(
    tmp_path: Path,
    target: str,
    mode: int,
    message: str,
) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "mode-canary", "mode-canary")
    path = root if target == "root" else root / target
    path.chmod(mode)

    with pytest.raises(AuthSecretError, match=message):
        load_admin_secrets(root)


def test_loader_rejects_symlink_secret_file(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "file-link-canary", "file-link-canary")
    key_path = root / SIGNING_KEY_FILENAME
    external = tmp_path / "external-key"
    external.write_text("x" * 64, encoding="utf-8")
    external.chmod(0o600)
    key_path.unlink()
    key_path.symlink_to(external)

    with pytest.raises(AuthSecretError, match="missing or unsafe"):
        load_admin_secrets(root)


def test_loader_rejects_non_argon2id_hash_and_short_signing_key(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "format-canary", "format-canary")
    hash_path = root / PASSWORD_HASH_FILENAME
    hash_path.write_text(PasswordHasher(type=Type.I).hash("format-canary"), encoding="utf-8")

    with pytest.raises(AuthSecretError, match="Argon2id"):
        load_admin_secrets(root)

    hash_path.write_text(PasswordHasher(type=Type.ID).hash("format-canary"), encoding="utf-8")
    (root / SIGNING_KEY_FILENAME).write_text("too-short", encoding="utf-8")
    with pytest.raises(AuthSecretError, match="signing key"):
        load_admin_secrets(root)


def test_loader_rejects_truncated_argon2id_hash(tmp_path: Path) -> None:
    root = tmp_path / "auth"
    initialize_admin_secrets(root, "truncated-canary", "truncated-canary")
    hash_path = root / PASSWORD_HASH_FILENAME
    hash_path.write_text("$argon2id$v=19$m=65536,t=3,p=4$broken$broken", encoding="utf-8")

    with pytest.raises(AuthSecretError, match="malformed"):
        load_admin_secrets(root)


def test_cli_uses_getpass_and_never_outputs_secret_material(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    root = tmp_path / "auth"
    password = "cli-password-canary"
    entered = iter([password, password])
    monkeypatch.setattr(cli, "getpass", lambda _: next(entered))

    cli.main([str(root)])

    captured = capsys.readouterr()
    loaded = load_admin_secrets(root)
    password_exposed = password in captured.out or password in captured.err
    hash_exposed = loaded.password_hash in captured.out
    key_exposed = loaded.signing_key in captured.out
    assert password_exposed is False
    assert hash_exposed is False
    assert key_exposed is False
    assert str(root) in captured.out
