import re
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.github import GitHubError, RateLimited
from repopilot.history import History, HistoryLink
from repopilot.issues import IssuePages

REPO = "owner/demo"
WEB = "https://github.com/" + REPO
SHA, OTHER = "a" * 40, "b" * 40
DATE = "2026-01-03T09:00:00Z"


def issue(number: int, pull: bool = False) -> dict[str, Any]:
    return {
        "number": number,
        "id": 100 + number,
        "title": f"Change {number}",
        "body": "Update the loader.",
        "user": {"login": "contributor"},
        "created_at": DATE,
        "updated_at": DATE,
        "state": "closed",
        "html_url": f"{WEB}/{'pull' if pull else 'issues'}/{number}",
        "labels": [{"name": "tests"}],
    }


def commit(sha: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "html_url": f"{WEB}/commit/{sha}",
        "commit": {
            "author": {"date": DATE, "name": "Author"},
            "committer": {"date": DATE},
            "message": "Change the loader",
        },
        "files": [{"filename": "src/loader.py"}],
    }


class Remote:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.version = 1
        self.limited = self.foreign = False
        self.deleted = False

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "api.github.com"
        path = request.url.path.removeprefix("/repos/owner/demo")
        page = request.url.params.get("page", "1")
        self.calls.append((path, page))
        if self.limited and path.endswith("timeline") and page == "2":
            return httpx2.Response(429)
        etag = (
            f'"v{self.version}"'
            if path.endswith("timeline") or self.foreign
            else '"v1"'
        )
        if request.headers.get("If-None-Match") == etag:
            return httpx2.Response(304)
        headers = {"ETag": etag}
        rows: Any
        if path == "/issues":
            first = issue(3)
            if self.foreign:
                first["html_url"] = first["html_url"].replace(REPO, "foreign/demo")
            rows = [first] if page == "1" else [issue(3), issue(4)]
        elif path == "/commits":
            rows = [commit(SHA), commit(OTHER)]
        elif path.startswith("/commits/"):
            rows = commit(path.split("/")[-1])
        elif path == "/issues/3":
            rows = issue(3)
        elif path.endswith("/timeline"):
            comment = {
                "event": "commented",
                "id": 19,
                "body": f"Observation {self.version}",
                "user": None,
                "created_at": DATE,
                "updated_at": f"2026-01-0{self.version + 3}T10:00:00Z",
                "html_url": f"{WEB}/issues/3#issuecomment-19",
            }
            source = issue(11, True) | {"pull_request": {"url": "unused"}}
            rows = (
                ([] if self.deleted else [comment])
                if page == "1"
                else [
                    {"event": "cross-referenced", "source": {"issue": source}},
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": source
                            | {"html_url": "https://github.com/other/r/pull/11"}
                        },
                    },
                    {
                        "event": "referenced",
                        "commit_id": SHA,
                        "commit_url": f"https://api.github.com/repos/{REPO}/commits/{SHA}",
                    },
                ]
            )
        elif path == "/pulls/11":
            rows = issue(11, True) | {"merged_at": DATE, "merge_commit_sha": SHA}
        elif path == "/pulls/11/files":
            rows = [{"filename": "src/loader.py"}]
        else:
            pytest.fail(f"Unexpected history request {path}")
        if page == "1" and (path.endswith("/timeline") or path == "/issues"):
            headers["Link"] = (
                '<https://api.github.com/repositories/10/items?page=2>; rel="next"'
            )
        return httpx2.Response(200, json=rows, headers=headers)


def test_scoped_history_deduplicates_and_persists(tmp_path: Path) -> None:
    remote = Remote()
    with httpx2.Client(transport=httpx2.MockTransport(remote.respond)) as client:
        service = History(tmp_path, client, lambda: 123)
        general = service.load(REPO, SHA)
        assert [r.key for r in general.records] == [
            "issue:3",
            "issue:4",
            "commit:" + SHA,
            "commit:" + OTHER,
        ]
        assert general.records[-1].paths == ("src/loader.py",) and not general.links
        assert len(remote.calls) == 5
        assert History(tmp_path, client).load(REPO.upper(), SHA) == general
        assert len(remote.calls) == 5
        detail = service.load(REPO, SHA, 3)
        by_key = {r.key: r for r in detail.records}
        assert by_key["issue:3"].id == "103" and by_key["issue:3"].labels == ("tests",)
        assert (
            by_key["comment:19"].author is None and by_key["pull:11"].state == "merged"
        )
        assert by_key["pull:11"].paths == ("src/loader.py",)
        assert all(
            r.url.startswith(WEB + "/") and r.timestamp == DATE for r in detail.records
        )
        assert detail.observed_at == 123 and detail.sha == SHA
        assert (
            HistoryLink(source="pull:11", relation="references", target="issue:3")
            in detail.links
        )
        assert (
            HistoryLink(source="pull:11", relation="merged_as", target="commit:" + SHA)
            in detail.links
        )
        assert remote.calls.count(("/commits/" + SHA, "1")) == 1
        assert not service.read("other/repo", SHA) and not service.read(REPO, OTHER)
        assert IssuePages(tmp_path, client).read(REPO).fetched_at is None


def test_refresh_is_atomic_and_observes_edits_and_deletions(tmp_path: Path) -> None:
    remote = Remote()
    with httpx2.Client(transport=httpx2.MockTransport(remote.respond)) as client:
        service = History(tmp_path, client)
        original = service.load(REPO, SHA, 3)
        remote.version = 2
        remote.limited = True
        with pytest.raises(RateLimited):
            service.load(REPO, SHA, 3, refresh=True)
        assert service.read(REPO, SHA, 3) == original
        remote.limited = False
        edited = service.load(REPO, SHA, 3, refresh=True)
        assert (
            next(r for r in edited.records if r.kind == "comment").body
            == "Observation 2"
        )
        remote.version, remote.deleted = 3, True
        deleted = service.load(REPO, SHA, 3, refresh=True)
        assert not any(r.kind == "comment" for r in deleted.records)
        first = service.load(REPO, SHA)
        remote.foreign = True
        with pytest.raises(GitHubError, match="invalid historical"):
            service.load(REPO, SHA, refresh=True)
        assert service.read(REPO, SHA) == first


def test_history_requires_explicit_form_and_preserves_queue(
    tmp_path: Path, explorer_url: str
) -> None:
    remote = Remote()
    url = explorer_url.removesuffix("/code") + "/issues"
    history = url + "/history"
    with TestClient(
        create_app(data_dir=tmp_path, transport=httpx2.MockTransport(remote.respond)),
        base_url="http://localhost",
    ) as client:
        assert client.get(url).is_success
        page = client.get(history, params={"number": 3})
        assert "Discussion for issue #3" in page.text
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token and not remote.calls
        assert (
            client.post(history, data={"number": 3, "csrf_token": "bad"}).status_code
            == 403
        )
        assert not remote.calls
        loaded = client.post(history, data={"number": 3, "csrf_token": token[1]})
        assert loaded.is_success and "Observation 1" in loaded.text
        assert "0 issues loaded" in client.get(url).text
        remote.version, remote.limited = 2, True
        limited = client.post(
            history, data={"number": 3, "csrf_token": token[1], "refresh": "true"}
        )
        assert limited.status_code == 429 and "Observation 1" in limited.text
