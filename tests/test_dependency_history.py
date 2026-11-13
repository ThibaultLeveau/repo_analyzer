import base64
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
    def __init__(self, scenario: str) -> None:
        self.scenario = scenario
        self.requests: list[httpx2.URL] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        url = request.url
        self.requests.append(url)
        assert url.host == "api.github.com"
        if self.scenario == "rate":
            return httpx2.Response(429)
        if url.path.endswith("/commits"):
            assert url.params["path"] == MANIFEST and url.params["per_page"] == "1"
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
                "parents": [{"sha": parent}],
                "commit": {"committer": {"date": "2025-01-02T03:04:05Z"}},
                "files": [{"filename": MANIFEST, "status": status}],
            }
            return httpx2.Response(200, json=info)
        assert url.path.endswith("/contents/" + MANIFEST)
        ref = url.params["ref"]
        text = project("==2")
        if ref == EDIT:
            text = "# latest unrelated manifest edit\n" + text
        elif ref == PARENT and self.scenario != "limit":
            text = project(None if self.scenario == "introduction" else "==1")
        raw = text.encode()
        return httpx2.Response(
            200,
            json={
                "type": "file",
                "path": MANIFEST,
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
