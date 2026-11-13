from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import httpx2
import pytest

from repopilot.github import GitHubError, RateLimited
from repopilot.issues import Cooldown, IssuePages, QueueBusy


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def row(number: int, *, pr: bool = False) -> dict[str, object]:
    return {
        "id": number,
        "number": number,
        "state": "open",
        "title": f"Issue {number}",
        "body": None if number % 2 else f"Details for {number}",
        "labels": [{"name": "feature" if number % 2 else "bug"}],
        "created_at": f"2026-01-01T00:{number // 60:02}:{number % 60:02}Z",
        "updated_at": "2026-01-01T01:00:00Z",
        **({"pull_request": {}} if pr else {}),
    }


class Pages:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.status = 200
        self.next_url = (
            "https://api.github.com/repositories/123/issues?page=2&per_page=100"
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        assert (
            request.url.host == "api.github.com"
            and request.url.path == "/repos/owner/demo/issues"
        )
        params = request.url.params
        assert (
            params["state"],
            params["sort"],
            params["direction"],
            params["per_page"],
        ) == ("open", "created", "desc", "100")
        page = int(params["page"])
        self.calls.append(page)
        if self.status != 200:
            return httpx2.Response(self.status, headers={"Retry-After": "80"})
        if page == 1:
            return httpx2.Response(
                200,
                json=[row(i, pr=i % 3 == 0) for i in range(150, 50, -1)],
                headers={"Link": f'<{self.next_url}>; rel="next"'},
            )
        assert page == 2
        return httpx2.Response(
            200, json=[row(52), *[row(i, pr=i % 3 == 0) for i in range(50, 0, -1)]]
        )


@pytest.fixture
def pages() -> Iterator[tuple[httpx2.Client, Pages]]:
    transport = Pages()
    with httpx2.Client(transport=httpx2.MockTransport(transport)) as client:
        yield client, transport


def test_queue_caches_leftovers_and_resumes_across_restart(
    tmp_path: Path, pages: tuple[httpx2.Client, Pages]
) -> None:
    client, fake = pages
    clock = Clock()
    service = IssuePages(tmp_path, client, clock)
    assert service.read("Owner/Demo").fetched_at is None and not fake.calls
    expected = [i for i in range(150, 0, -1) if i % 3]
    first = service.load("Owner/Demo")
    assert [i.id for i in first.issues] == expected[:50] and fake.calls == [1]
    assert first.issues[0].labels == ["feature"] and first.issues[0].body == ""
    reopened = IssuePages(tmp_path, client, clock)
    assert reopened.load("owner/demo") == first
    with pytest.raises(Cooldown) as exc:
        reopened.load("owner/demo", "extend")
    assert exc.value.until == clock.now + 6 * 3600 and fake.calls == [1]
    reopened.set_interval("owner/demo", 0.5)
    clock.now += 1800
    rest = reopened.load("owner/demo", "extend")
    assert [i.id for i in rest.issues] == expected
    assert rest.exhausted and fake.calls == [1, 2]
    assert reopened.load("owner/demo", "extend") == rest and fake.calls == [1, 2]
    clock.now += 1800
    fresh = reopened.load("owner/demo", "refresh")
    assert (
        fresh.issues == first.issues and not fresh.exhausted and fake.calls == [1, 2, 1]
    )


def test_rate_limit_and_failed_refresh_leave_previous_window_intact(
    tmp_path: Path, pages: tuple[httpx2.Client, Pages]
) -> None:
    client, fake = pages
    clock = Clock()
    service = IssuePages(tmp_path, client, clock)
    previous = service.load("owner/demo")
    clock.now += 7 * 3600
    fake.status = 429
    with pytest.raises(RateLimited):
        service.load("owner/demo", "refresh")
    reopened = IssuePages(tmp_path, client, clock)
    limited = reopened.read("owner/demo")
    assert limited.retry_at == clock.now + 80 and limited.issues == previous.issues
    assert limited.fetched_at == previous.fetched_at
    with pytest.raises(Cooldown):
        reopened.load("owner/demo", "extend")
    clock.now += 80
    fake.status = 503
    with pytest.raises(GitHubError, match="503"):
        reopened.load("owner/demo", "refresh")
    assert reopened.read("owner/demo").issues == previous.issues
    fake.status = 200
    fake.next_url = "https://elsewhere.example/repos/owner/demo/issues?page=2"
    with pytest.raises(GitHubError, match="pagination"):
        reopened.load("owner/demo", "refresh")
    assert (
        fake.calls == [1, 1, 1, 1]
        and reopened.read("owner/demo").issues == previous.issues
    )
    for invalid in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            reopened.set_interval("owner/demo", invalid)


def test_pr_only_page_and_concurrent_load(tmp_path: Path) -> None:
    started, release = Event(), Event()
    calls = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.params["page"])
        if len(calls) == 1:
            started.set()
            assert release.wait(timeout=10)
            return httpx2.Response(
                200,
                json=[row(5, pr=True)],
                headers={
                    "Link": '<https://api.github.com/repos/Owner/Demo/issues?page=2>; rel="next"'
                },
            )
        return httpx2.Response(200, json=[row(4), row(4), row(3)])

    with (
        httpx2.Client(transport=httpx2.MockTransport(respond)) as client,
        ThreadPoolExecutor(1) as pool,
    ):
        service = IssuePages(tmp_path, client)
        future = pool.submit(service.load, "Owner/Demo")
        try:
            assert started.wait(timeout=10)
            assert service.read("owner/demo").issues == ()
            with pytest.raises(QueueBusy):
                IssuePages(tmp_path, client).load("owner/demo")
        finally:
            release.set()
        result = future.result(timeout=10)
    assert (
        [i.id for i in result.issues] == [4, 3]
        and result.exhausted
        and calls == ["1", "2"]
    )
