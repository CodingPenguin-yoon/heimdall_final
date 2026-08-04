from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


class GitAccessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    author_name: str
    committed_at: datetime
    subject: str


class GitClient:
    def __init__(
        self,
        *,
        executable: str = "git",
        timeout_seconds: float = 20,
        recent_commit_limit: int = 20,
    ) -> None:
        self._executable = executable
        self._timeout_seconds = timeout_seconds
        self._recent_commit_limit = recent_commit_limit

    def validate_main(self, repository_url: str) -> None:
        validate_public_github_url(repository_url)
        result = self._run(
            "ls-remote",
            "--exit-code",
            "--heads",
            repository_url,
            "refs/heads/main",
        )
        if not result.stdout.strip():
            raise GitAccessError("Public repository does not expose a main branch")

    def recent_commits(self, repository_url: str) -> list[Commit]:
        validate_public_github_url(repository_url)
        with tempfile.TemporaryDirectory(prefix="heimdall-git-") as temporary:
            root = Path(temporary)
            self._run("init", "--quiet", str(root))
            self._run(
                "-C",
                str(root),
                "fetch",
                "--quiet",
                f"--depth={self._recent_commit_limit}",
                repository_url,
                "+refs/heads/main:refs/remotes/origin/main",
            )
            result = self._run(
                "-C",
                str(root),
                "log",
                f"--max-count={self._recent_commit_limit}",
                "--format=%H%x00%an%x00%aI%x00%s%x1e",
                "refs/remotes/origin/main",
            )
        return _parse_log(result.stdout)

    def checkout_exact(self, repository_url: str, commit_sha: str, target: Path) -> None:
        validate_public_github_url(repository_url)
        if re.fullmatch(r"[0-9a-f]{40}", commit_sha) is None:
            raise GitAccessError("Commit SHA must be 40 lowercase hexadecimal characters")
        if target.exists():
            if target.is_symlink() or not target.is_dir() or any(target.iterdir()):
                raise GitAccessError("Source workspace must be an empty private directory")
        else:
            target.mkdir(mode=0o700, parents=True)
        os.chmod(target, 0o700)

        self._run("init", "--quiet", str(target))
        self._run("-C", str(target), "remote", "add", "origin", repository_url)
        self._run(
            "-C",
            str(target),
            "fetch",
            "--quiet",
            f"--depth={self._recent_commit_limit}",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        )
        self._run("-C", str(target), "cat-file", "-e", f"{commit_sha}^{{commit}}")
        self._run(
            "-C",
            str(target),
            "merge-base",
            "--is-ancestor",
            commit_sha,
            "refs/remotes/origin/main",
        )
        self._run("-C", str(target), "checkout", "--quiet", "--detach", commit_sha)

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        }
        try:
            return subprocess.run(
                [self._executable, *arguments],
                check=True,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=self._timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise GitAccessError("Unable to read the public Git repository") from error


def validate_public_github_url(repository_url: str) -> None:
    parsed = urlsplit(repository_url)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        raise GitAccessError("Only public https://github.com repositories are supported")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise GitAccessError("Repository URL must not contain credentials, query, or fragment")
    if parsed.port not in (None, 443):
        raise GitAccessError("Repository URL must use the default HTTPS port")
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) != 2 or any(part in {".", ".."} for part in path_parts):
        raise GitAccessError("Repository URL must identify one GitHub owner and repository")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return
    if not address.is_global:
        raise GitAccessError("Local and private repository hosts are not supported")


def _parse_log(output: str) -> list[Commit]:
    commits: list[Commit] = []
    for record in output.split("\x1e"):
        values = record.strip().split("\x00")
        if len(values) != 4:
            continue
        sha, author_name, committed_at, subject = values
        if len(sha) != 40:
            continue
        commits.append(
            Commit(
                sha=sha,
                author_name=author_name[:200],
                committed_at=datetime.fromisoformat(committed_at),
                subject=" ".join(subject.split())[:300],
            )
        )
    return commits
