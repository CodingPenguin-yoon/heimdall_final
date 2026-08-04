import os
from pathlib import Path

import pytest

from heimdall.git.client import GitClient

REPOSITORY_URL = os.environ.get("HEIMDALL_TEST_PUBLIC_REPOSITORY_URL")

pytestmark = pytest.mark.skipif(
    not REPOSITORY_URL,
    reason="Public Git checkout smoke repository is not configured",
)


def test_recent_main_commit_is_checked_out_detached_and_exact(tmp_path: Path) -> None:
    assert REPOSITORY_URL is not None
    client = GitClient(timeout_seconds=30, recent_commit_limit=20)
    commits = client.recent_commits(REPOSITORY_URL)
    assert commits
    target = tmp_path / "source"

    client.checkout_exact(REPOSITORY_URL, commits[0].sha, target)

    assert (target / ".git" / "HEAD").read_text().strip() == commits[0].sha
