import httpx2
import pytest

from repopilot.github import (
    GitHubError,
    RateLimited,
    RepositoryHead,
    resolve_repository,
)

URL = "https://github.com/old/project"
SHA = "a1" * 20
METADATA = {
    "private": False,
    "full_name": "New/Project",
    "default_branch": "release/stable",
}


@pytest.mark.parametrize("suffix", ["", "/", ".git", ".git/"])
def test_public_repo_renamed_and_nonstandard_default(suffix: str) -> None:
    paths: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.method == "GET" and "authorization" not in request.headers
        paths.append(request.url.raw_path)
        if len(paths) == 1:
            return httpx2.Response(301, headers={"Location": "/repositories/42"})
        return httpx2.Response(200, json=METADATA if len(paths) == 2 else {"sha": SHA})

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        assert resolve_repository(URL + suffix, client) == RepositoryHead(
            "New/Project", "release/stable", SHA
        )
    assert paths == [
        b"/repos/old/project",
        b"/repositories/42",
        b"/repos/New/Project/commits/release%2Fstable",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b",
        "https://example.com/a/b",
        "https://x@github.com/a/b",
        "https://github.com:8080/a/b",
        "https://github.com/a/..",
        "https://github.com/a/b/issues",
        "https://github.com/a/b?ref=main",
        "https://github.com/a/b#file",
        "https://[bad",
    ],
)
def test_invalid_input_makes_no_request(url: str) -> None:
    def unexpected(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("Invalid input reached the transport")

    with (
        httpx2.Client(transport=httpx2.MockTransport(unexpected)) as client,
        pytest.raises(GitHubError),
    ):
        resolve_repository(url, client)


def test_untrusted_redirect_is_rejected_even_with_redirects_enabled() -> None:
    calls = 0

    def redirect(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(302, headers={"Location": "https://example.org/test"})

    with (
        httpx2.Client(
            transport=httpx2.MockTransport(redirect), follow_redirects=True
        ) as client,
        pytest.raises(GitHubError, match="origin"),
    ):
        resolve_repository(URL, client)
    assert calls == 1


@pytest.mark.parametrize(
    ("responses", "message"),
    [
        ([httpx2.Response(404)], "unavailable"),
        ([httpx2.Response(200, json={"private": True})], "public"),
        ([httpx2.Response(200, text="not json")], "invalid JSON"),
        ([httpx2.Response(200, json=[])], "format"),
        ([httpx2.Response(200, json=METADATA | {"full_name": "a/.."})], "name"),
        (
            [httpx2.Response(200, json=METADATA | {"default_branch": ""})],
            "no default branch",
        ),
        (
            [httpx2.Response(200, json=METADATA), httpx2.Response(409)],
            "no available commit",
        ),
        (
            [
                httpx2.Response(200, json=METADATA),
                httpx2.Response(200, json={"sha": "a1"}),
            ],
            "commit SHA",
        ),
    ],
)
def test_invalid_responses(responses: list[httpx2.Response], message: str) -> None:
    replies = iter(responses)
    with (
        httpx2.Client(
            transport=httpx2.MockTransport(lambda _: next(replies))
        ) as client,
        pytest.raises(GitHubError, match=message),
    ):
        resolve_repository(URL, client)


def test_timeout() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timeout")

    with (
        httpx2.Client(transport=httpx2.MockTransport(timeout)) as client,
        pytest.raises(GitHubError, match="timed out"),
    ):
        resolve_repository(URL, client)


def test_rate_limit_metadata() -> None:
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "12345",
        "Retry-After": "30",
    }
    with (
        httpx2.Client(
            transport=httpx2.MockTransport(
                lambda _: httpx2.Response(403, headers=headers)
            )
        ) as client,
        pytest.raises(RateLimited) as error,
    ):
        resolve_repository(URL, client)
    assert (error.value.reset_at, error.value.retry_after) == ("12345", "30")
