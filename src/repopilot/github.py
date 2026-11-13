import re
from urllib.parse import quote, urlsplit

import httpx2

from repopilot.models import RepositoryHead

__all__ = [
    "GitHubError",
    "RateLimited",
    "RepositoryHead",
    "api_get",
    "repository_name",
    "resolve_repository",
]

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_API = "https://api.github.com"


class GitHubError(ValueError):
    """A repository lookup could not be completed."""


class RateLimited(GitHubError):
    def __init__(self, headers: httpx2.Headers) -> None:
        super().__init__("GitHub rate limit reached. Try again after the limit resets.")
        self.reset_at = headers.get("x-ratelimit-reset")
        self.retry_after = headers.get("retry-after")


def _valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and name.split("/")[1] not in {".", ".."}


def repository_name(url: str) -> str:
    text = url.strip()
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        raise GitHubError("Invalid GitHub repository URL") from exc
    name = parts.path.removeprefix("/").removesuffix("/").removesuffix(".git")
    if (
        parts.scheme != "https"
        or parts.netloc.lower() != "github.com"
        or parts.query
        or parts.fragment
        or any(ord(c) < 32 for c in text)
        or not _valid_name(name)
    ):
        raise GitHubError("Use a public https://github.com/owner/repository URL")
    return name


def api_get(client: httpx2.Client, url: str) -> httpx2.Response:
    try:
        target = httpx2.URL(url)
        for _ in range(6):
            if (
                target.scheme != "https"
                or target.host != "api.github.com"
                or target.port is not None
                or target.userinfo
            ):
                raise GitHubError("Unexpected GitHub API origin")
            response = client.get(
                target,
                headers={"Accept": "application/vnd.github+json"},
                timeout=15,
                follow_redirects=False,
            )
            if response.has_redirect_location:
                target = target.join(response.headers["location"])
                continue
            if response.status_code == 429 or (
                response.status_code == 403
                and (
                    response.headers.get("x-ratelimit-remaining") == "0"
                    or "retry-after" in response.headers
                    or "rate limit" in response.text.lower()
                )
            ):
                raise RateLimited(response.headers)
            if response.status_code in (403, 404):
                raise GitHubError("Public repository or revision is unavailable")
            if response.status_code == 409:
                raise GitHubError("Repository has no available commit")
            if not response.is_success:
                raise GitHubError(f"GitHub returned HTTP {response.status_code}")
            return response
    except httpx2.InvalidURL as exc:
        raise GitHubError("Invalid GitHub API URL") from exc
    except httpx2.TimeoutException as exc:
        raise GitHubError("GitHub request timed out") from exc
    except httpx2.RequestError as exc:
        raise GitHubError("Could not reach GitHub") from exc
    raise GitHubError("GitHub returned too many redirects")


def _object(response: httpx2.Response) -> dict[str, object]:
    try:
        data = response.json()
    except ValueError as exc:
        raise GitHubError("GitHub returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise GitHubError("Unexpected GitHub response format")
    return data


def resolve_repository(
    url: str, client: httpx2.Client, *, revision: str | None = None
) -> RepositoryHead:
    name = repository_name(url)
    if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise GitHubError("Expected a full commit SHA")
    metadata = _object(api_get(client, f"{_API}/repos/{name}"))
    if metadata.get("private") is not False:
        raise GitHubError("GitHub did not identify a public repository")
    canonical, branch = metadata.get("full_name"), metadata.get("default_branch")
    if not isinstance(canonical, str) or not _valid_name(canonical):
        raise GitHubError("GitHub returned an invalid repository name")
    if not isinstance(branch, str) or not branch.strip():
        raise GitHubError("Repository has no default branch")
    commit = _object(
        api_get(
            client,
            f"{_API}/repos/{canonical}/commits/{quote(revision or branch, safe='')}",
        )
    )
    sha = commit.get("sha")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise GitHubError("GitHub returned an invalid commit SHA")
    if revision and sha != revision:
        raise GitHubError("GitHub returned a different revision")
    return RepositoryHead(canonical, branch, sha)
