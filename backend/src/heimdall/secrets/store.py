from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol


@dataclass(frozen=True, slots=True)
class StoredSecret:
    reference: str
    version: int
    fingerprint: str


class SecretStore(Protocol):
    def create(
        self, reference_root: str, version: int, value: str | None = None
    ) -> StoredSecret: ...

    def read(self, reference: str, fingerprint: str) -> str: ...


class SecretStoreError(RuntimeError):
    pass


class FileSecretStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def create(self, reference_root: str, version: int, value: str | None = None) -> StoredSecret:
        relative_root = _safe_relative(reference_root)
        directory = self._resolve(relative_root)
        _ensure_private_directory(self._root)
        _ensure_private_directory(directory)

        target = directory / f"v{version}.secret"
        secret_value = value if value is not None else secrets.token_urlsafe(32)
        payload = secret_value.encode("utf-8")
        fingerprint = hashlib.sha256(payload).hexdigest()

        if target.exists():
            existing = _read_private_file(target)
            existing_fingerprint = hashlib.sha256(existing).hexdigest()
            if existing_fingerprint != fingerprint and value is not None:
                raise SecretStoreError("secret version already exists with different content")
            fingerprint = existing_fingerprint
        else:
            _write_once(target, payload)

        reference = (relative_root / target.name).as_posix()
        return StoredSecret(reference=reference, version=version, fingerprint=fingerprint)

    def read(self, reference: str, fingerprint: str) -> str:
        path = self._resolve(_safe_relative(reference))
        payload = _read_private_file(path)
        if hashlib.sha256(payload).hexdigest() != fingerprint:
            raise SecretStoreError("secret fingerprint does not match metadata")
        return payload.decode("utf-8")

    def _resolve(self, relative: PurePosixPath) -> Path:
        candidate = self._root.joinpath(*relative.parts)
        resolved_parent = candidate.parent.resolve()
        if not resolved_parent.is_relative_to(self._root):
            raise SecretStoreError("secret path escapes runtime root")
        return resolved_parent / candidate.name


def _safe_relative(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SecretStoreError("secret reference must be a canonical relative path")
    return path


def _ensure_private_directory(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise SecretStoreError("secret directory is not a private directory")
            continue
        current.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _write_once(target: Path, payload: bytes) -> None:
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o400)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(temporary, target, follow_symlinks=False)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    os.chmod(target, 0o400, follow_symlinks=False)


def _read_private_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise SecretStoreError("secret file is missing or unsafe")
    if path.stat().st_mode & 0o077:
        raise SecretStoreError("secret file permissions are too broad")
    return path.read_bytes()
