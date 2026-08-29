from __future__ import annotations

import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import UUID


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

    def resolve(self, reference: str, fingerprint: str) -> Path: ...

    def project_operation_lock(
        self, project_id: UUID, *, blocking: bool = True
    ) -> Iterator[None]: ...

    def delete_project_subtree(self, project_id: UUID) -> None: ...

    def project_subtree_absent(self, project_id: UUID) -> bool: ...


class SecretStoreError(RuntimeError):
    pass


class SecretStoreBusyError(SecretStoreError):
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
        path = self.resolve(reference, fingerprint)
        payload = _read_private_file(path)
        return payload.decode("utf-8")

    def resolve(self, reference: str, fingerprint: str) -> Path:
        path = self._resolve(_safe_relative(reference))
        payload = _read_private_file(path)
        if hashlib.sha256(payload).hexdigest() != fingerprint:
            raise SecretStoreError("secret fingerprint does not match metadata")
        return path

    @contextmanager
    def project_operation_lock(self, project_id: UUID, *, blocking: bool = True) -> Iterator[None]:
        lock_root = self._root / ".locks" / "projects"
        _ensure_private_directory_strict(self._root, self._root)
        _ensure_private_directory_strict(lock_root, self._root)
        lock_path = lock_root / f"{project_id}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise SecretStoreError("project operation lock is unsafe")
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(descriptor, operation)
            except BlockingIOError as error:
                raise SecretStoreBusyError("project secret operation is active") from error
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def delete_project_subtree(self, project_id: UUID) -> None:
        target_name = str(project_id)
        try:
            root_descriptor = _open_private_directory(self._root)
        except FileNotFoundError:
            return
        try:
            try:
                projects_descriptor = _open_private_directory_at(root_descriptor, "projects")
            except FileNotFoundError:
                return
            try:
                try:
                    target_descriptor = _open_private_directory_at(projects_descriptor, target_name)
                except FileNotFoundError:
                    return
                identity = os.fstat(target_descriptor)
                try:
                    _validate_tree(target_descriptor)
                    _delete_tree(target_descriptor)
                    current = os.stat(
                        target_name,
                        dir_fd=projects_descriptor,
                        follow_symlinks=False,
                    )
                    if (current.st_dev, current.st_ino) != (
                        identity.st_dev,
                        identity.st_ino,
                    ):
                        raise SecretStoreError("project secret subtree identity changed")
                finally:
                    os.close(target_descriptor)
                os.rmdir(target_name, dir_fd=projects_descriptor)
                try:
                    os.stat(
                        target_name,
                        dir_fd=projects_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise SecretStoreError("project secret subtree deletion was not confirmed")
            finally:
                os.close(projects_descriptor)
        except (OSError, SecretStoreError) as error:
            if isinstance(error, SecretStoreError):
                raise
            raise SecretStoreError("unsafe project secret subtree") from error
        finally:
            os.close(root_descriptor)

    def project_subtree_absent(self, project_id: UUID) -> bool:
        try:
            root_descriptor = _open_private_directory(self._root)
        except FileNotFoundError:
            return True
        try:
            try:
                projects_descriptor = _open_private_directory_at(root_descriptor, "projects")
            except FileNotFoundError:
                return True
            try:
                try:
                    target_descriptor = _open_private_directory_at(
                        projects_descriptor, str(project_id)
                    )
                except FileNotFoundError:
                    return True
                try:
                    _validate_tree(target_descriptor)
                    return False
                finally:
                    os.close(target_descriptor)
            finally:
                os.close(projects_descriptor)
        except OSError as error:
            raise SecretStoreError("unsafe project secret subtree") from error
        finally:
            os.close(root_descriptor)

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


def _ensure_private_directory_strict(path: Path, boundary: Path) -> None:
    if not boundary.exists() and not boundary.is_symlink():
        boundary.mkdir(mode=0o700, parents=True)
    _validate_private_directory(boundary)
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise SecretStoreError("project operation lock escapes runtime root") from error
    current = boundary
    for part in relative.parts:
        current /= part
        if current.exists() or current.is_symlink():
            _validate_private_directory(current)
        else:
            current.mkdir(mode=0o700)


def _validate_private_directory(path: Path) -> None:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SecretStoreError("unsafe project secret subtree")


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise SecretStoreError("unsafe project secret subtree")
    return descriptor


def _open_private_directory_at(parent: int, name: str) -> int:
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent,
    )
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise SecretStoreError("unsafe project secret subtree")
    return descriptor


def _validate_tree(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise SecretStoreError("unsafe project secret subtree")
        if stat.S_ISREG(metadata.st_mode):
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise SecretStoreError("unsafe project secret subtree")
        child = _open_private_directory_at(descriptor, name)
        try:
            _validate_tree(child)
        finally:
            os.close(child)


def _delete_tree(descriptor: int) -> None:
    for name in os.listdir(descriptor):
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISREG(metadata.st_mode):
            file_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            try:
                identity = os.fstat(file_descriptor)
                _validate_private_entry(identity, directory=False)
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if not _same_identity(identity, current):
                    raise SecretStoreError("project secret subtree identity changed")
                os.unlink(name, dir_fd=descriptor)
            finally:
                os.close(file_descriptor)
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            raise SecretStoreError("unsafe project secret subtree")
        child = _open_private_directory_at(descriptor, name)
        identity = os.fstat(child)
        try:
            _delete_tree(child)
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if not _same_identity(identity, current):
                raise SecretStoreError("project secret subtree identity changed")
        finally:
            os.close(child)
        os.rmdir(name, dir_fd=descriptor)


def _validate_private_entry(metadata: os.stat_result, *, directory: bool) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected_type(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise SecretStoreError("unsafe project secret subtree")


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


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
