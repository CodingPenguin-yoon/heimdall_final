import pytest

from heimdall.git.client import GitAccessError, validate_public_github_url


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example/repo",
        "https://token@github.com/example/repo",
        "https://github.com/example/repo?token=secret",
        "https://gitlab.com/example/repo",
        "https://github.com/example",
        "https://github.com/example/repo/extra",
    ],
)
def test_public_url_policy_rejects_unsupported_sources(url: str) -> None:
    with pytest.raises(GitAccessError):
        validate_public_github_url(url)


def test_public_url_policy_accepts_one_github_repository() -> None:
    validate_public_github_url("https://github.com/example/repo.git")
