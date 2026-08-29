from __future__ import annotations

import hashlib
import os
import secrets
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher, Type, extract_parameters
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

PASSWORD_HASH_FILENAME = "admin-password.hash"
SIGNING_KEY_FILENAME = "session-signing.key"
MINIMUM_SIGNING_KEY_BYTES = 32
MAXIMUM_SECRET_FILE_BYTES = 4096
MAXIMUM_PASSWORD_LENGTH = 1024


class AuthSecretError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AdminSecrets:
    password_hash: str
    signing_key: str
    credential_revision: str


def initialize_admin_secrets(root: Path, password: str, confirmation: str) -> None:
    target = _absolute(root)
    _require_missing_target(target)
    if password != confirmation:
        raise AuthSecretError("password confirmation does not match")
    if not password:
        raise AuthSecretError("password must not be empty")
    if len(password) > MAXIMUM_PASSWORD_LENGTH:
        raise AuthSecretError("password is too long")

    target.parent.mkdir(parents=True, exist_ok=True)
    _require_no_symlink_components(target)
    _require_missing_target(target)
    _require_outside_git_worktree(target)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        os.chmod(temporary, 0o700)
        password_hash = PasswordHasher(type=Type.ID).hash(password)
        signing_key = secrets.token_urlsafe(48)
        _write_new_private_file(
            temporary / PASSWORD_HASH_FILENAME,
            password_hash.encode("utf-8"),
        )
        _write_new_private_file(
            temporary / SIGNING_KEY_FILENAME,
            signing_key.encode("utf-8"),
        )
        _fsync_directory(temporary)
        _require_missing_target(target)
        os.rename(temporary, target)
        _fsync_directory(target.parent)
    except BaseException:
        _remove_initialization_directory(temporary)
        raise


def load_admin_secrets(root: Path) -> AdminSecrets:
    directory = _absolute(root)
    metadata = _lstat(directory, "authentication secret directory is missing")
    if stat.S_ISLNK(metadata.st_mode):
        raise AuthSecretError("authentication secret directory must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise AuthSecretError("authentication secret directory must be a 0700 directory")

    password_hash_bytes = _read_private_file(directory / PASSWORD_HASH_FILENAME)
    signing_key_bytes = _read_private_file(directory / SIGNING_KEY_FILENAME)
    try:
        password_hash = password_hash_bytes.decode("utf-8")
        signing_key = signing_key_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AuthSecretError("authentication secret file is not valid UTF-8") from error

    if not password_hash or password_hash != password_hash.strip():
        raise AuthSecretError("administrator password hash is malformed")
    try:
        parameters = extract_parameters(password_hash)
    except InvalidHashError as error:
        raise AuthSecretError("administrator password hash is malformed") from error
    if parameters.type is not Type.ID:
        raise AuthSecretError("administrator password hash must use Argon2id")
    try:
        PasswordHasher().verify(password_hash, "heimdall-auth-format-validation")
    except VerifyMismatchError:
        pass
    except VerificationError as error:
        raise AuthSecretError("administrator password hash is malformed") from error
    if (
        len(signing_key_bytes) < MINIMUM_SIGNING_KEY_BYTES
        or signing_key != signing_key.strip()
        or "\x00" in signing_key
    ):
        raise AuthSecretError("session signing key is malformed")

    return AdminSecrets(
        password_hash=password_hash,
        signing_key=signing_key,
        credential_revision=hashlib.sha256(password_hash_bytes).hexdigest(),
    )


def _absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise AuthSecretError("authentication secret directory must be absolute")
    return path


def _path_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def _require_missing_target(path: Path) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode):
        raise AuthSecretError("authentication secret directory must not be a symlink")
    raise AuthSecretError("authentication secret directory already exists")


def _require_no_symlink_components(path: Path) -> None:
    for component in (*reversed(path.parents), path):
        try:
            metadata = os.lstat(component)
        except FileNotFoundError as error:
            if component == path:
                continue
            raise AuthSecretError("authentication secret directory ancestor is missing") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise AuthSecretError("authentication secret path must not contain symlinks")


def _require_outside_git_worktree(path: Path) -> None:
    for ancestor in path.parents:
        if _path_exists(ancestor / ".git"):
            raise AuthSecretError("authentication secret directory must be outside a Git worktree")


def _lstat(path: Path, missing_message: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except FileNotFoundError as error:
        raise AuthSecretError(missing_message) from error


def _read_private_file(path: Path) -> bytes:
    path_metadata = _lstat(path, "authentication secret file is missing or unsafe")
    if stat.S_ISLNK(path_metadata.st_mode):
        raise AuthSecretError("authentication secret file is missing or unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except (FileNotFoundError, OSError) as error:
        raise AuthSecretError("authentication secret file is missing or unsafe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AuthSecretError("authentication secret files must be regular 0600 files")
        payload = os.read(descriptor, MAXIMUM_SECRET_FILE_BYTES + 1)
        if len(payload) > MAXIMUM_SECRET_FILE_BYTES:
            raise AuthSecretError("authentication secret file is too large")
        return payload
    finally:
        os.close(descriptor)


def _write_new_private_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_initialization_directory(path: Path) -> None:
    if not _path_exists(path):
        return
    for filename in (PASSWORD_HASH_FILENAME, SIGNING_KEY_FILENAME):
        with suppress(FileNotFoundError):
            os.unlink(path / filename)
    with suppress(FileNotFoundError):
        os.rmdir(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
