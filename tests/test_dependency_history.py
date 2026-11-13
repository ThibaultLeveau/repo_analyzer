import base64
import sqlite3
from contextlib import closing
from pathlib import Path

import httpx2
import pytest
from test_manifests import manifest_snapshot

from repopilot.artifacts import Artifacts
from repopilot.dependency_history import REQUEST_LIMIT, dependency_history
from repopilot.manifests import parse_manifests

HEAD, EDIT, CHANGE, PARENT = (c * 40 for c in "abcd")
MANIFEST = "pyproject.toml"


def project(version: str | None) -> str:
    item = f"'example{version}'" if version else ""
    return f"[project]\ndependencies = [{item}]\n"


class HistoryFixture:
    path = MANIFEST

    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.requests: list[httpx2.URL] = []

    def content(self, ref: str) -> str:
        if ref == EDIT:
            return "# latest unrelated manifest edit\n" + project("==2")
        if ref == PARENT and self.scenario != "limit":
            return project(None if self.scenario == "introduction" else "==1")
        return project("==2")

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.requests.append(url)
        assert url.host == "api.github.com"
        if self.scenario == "rate":
            return httpx2.Response(429)
        if url.path.endswith("/commits"):
            assert url.params["path"] == self.path and url.params["per_page"] == "1"
            if self.scenario == "shallow":
                return httpx2.Response(200, json=[])
            commit = EDIT if url.params["sha"] == HEAD else url.params["sha"]
            return httpx2.Response(200, json=[{"sha": commit}])
        if "/commits/" in url.path:
            commit = url.path.rsplit("/", 1)[1]
            parent = CHANGE if commit == EDIT else PARENT
            if self.scenario == "limit":
                parent = f"{len(self.requests):040x}"
            status = {"rename": "renamed", "added": "added"}.get(
                self.scenario, "modified"
            )
            info = {
                "sha": commit,
                "parents": []
                if self.scenario == "missing-parent"
                else [{"sha": parent}],
                "commit": {"committer": {"date": "2025-01-02T03:04:05Z"}},
                "files": [{"filename": self.path, "status": status}],
            }
            return httpx2.Response(200, json=info)
        assert url.path.endswith("/contents/" + self.path)
        ref = url.params["ref"]
        raw = self.content(ref).encode()
        return httpx2.Response(
            200,
            json={
                "type": "file",
                "path": self.path,
                "size": len(raw),
                "encoding": "base64",
                "content": base64.b64encode(raw).decode(),
            },
        )


@pytest.mark.parametrize(
    ("scenario", "description", "commit"),
    [
        ("change", "Declared version changed", CHANGE),
        ("introduction", "Declaration introduced", CHANGE),
        ("added", "Declaration introduced", EDIT),
        ("rename", "Unknown", ""),
        ("shallow", "Unknown", ""),
        ("rate", "Unknown. GitHub rate limit", ""),
        ("limit", "Unknown", ""),
    ],
)
def test_bounded_version_attribution_and_restart_cache(
    tmp_path: Path, scenario: str, description: str, commit: str
) -> None:
    snapshot = manifest_snapshot(tmp_path, {MANIFEST: project("==2")})
    (selected,) = parse_manifests(snapshot).declarations
    fixture = HistoryFixture(scenario)
    with httpx2.Client(transport=httpx2.MockTransport(fixture.handle)) as client:
        result = dependency_history(
            snapshot, Artifacts(), client, "owner/repo", HEAD, selected
        )
        assert result.description.startswith(description) and result.commit == commit
        if commit:
            assert result.date == "2025-01-02" and "example==2" in result.source
            assert result.line == (3 if commit == EDIT else 2)
        count = len(fixture.requests)
        assert count <= REQUEST_LIMIT
        if scenario == "limit":
            assert count == REQUEST_LIMIT
        assert (
            dependency_history(
                snapshot, Artifacts(), client, "owner/repo", HEAD, selected
            )
            == result
        )
        assert len(fixture.requests) == count + (scenario == "rate")


class GoHistory(HistoryFixture):
    path = "service/go.mod"
    module = "example.org/lib/v2"
    version = "v2.1.0-0.20250101093000-abcdefabcdef"

    def content(self, ref: str) -> str:
        version = "v2.0.0" if ref == PARENT else self.version
        line = f"require {self.module} {version}\n"
        return "module example.org/app\n" + ("\n" if ref == EDIT else "") + line


@pytest.mark.parametrize(
    "scenario,commit", [("change", CHANGE), ("missing-parent", "")]
)
def test_go_requirement_history_refreshes_old_unknown_result(
    tmp_path: Path, scenario: str, commit: str
) -> None:
    fixture = GoHistory(scenario)
    snapshot = manifest_snapshot(tmp_path, {fixture.path: fixture.content(EDIT)})
    (selected,) = parse_manifests(snapshot).declarations
    assert selected.name == "go:" + fixture.module and selected.span.start == 3
    cache = Artifacts()
    with httpx2.Client(transport=httpx2.MockTransport(fixture.handle)) as client:
        result = dependency_history(
            snapshot, cache, client, "owner/repo", HEAD, selected
        )
        assert result.commit == commit
        if commit:
            assert (
                result.line == 2
                and result.source == fixture.content(CHANGE).splitlines()[1]
            )
            assert result.date == "2025-01-02" and fixture.version in result.source
        else:
            assert result.description.startswith("Unknown")
        count = len(fixture.requests)
        assert count <= REQUEST_LIMIT
        assert (
            dependency_history(snapshot, cache, client, "owner/repo", HEAD, selected)
            == result
        )
        assert len(fixture.requests) == count
        with (
            closing(sqlite3.connect(snapshot.database.parent / "analysis.db")) as db,
            db,
        ):
            db.execute(
                "UPDATE artifacts SET version='1',payload=? WHERE kind LIKE 'history:%'",
                (b'{"description":"Obsolete cached result"}',),
            )
        assert (
            dependency_history(snapshot, cache, client, "owner/repo", HEAD, selected)
            == result
        )
        assert len(fixture.requests) > count
