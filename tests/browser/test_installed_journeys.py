import json
import shutil
import subprocess
import sysconfig
from pathlib import Path

import httpx2
import pytest
from journey_fixture import FILES, REPO, Catalog, CatalogVectors
from langchain_core.messages import BaseMessage
from playwright.sync_api import Page, expect
from test_history import REPO as HISTORY_REPO
from test_issues import row
from test_manifests import manifest_snapshot
from test_refresh import Upstream
from test_timeline import REASON, Discussion, HistoryModel


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def import_transport(upstream: Upstream) -> httpx2.MockTransport:
    history = Discussion()

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith("/repos/" + HISTORY_REPO):
            if request.url.params.get("state") == "open":
                upstream.catalog.issue_requests += 1
                return httpx2.Response(
                    200,
                    json=[
                        row(20) | {"title": "Improve catalog validation in src/demo.py"}
                    ],
                )
            result = history.respond(request)
            return httpx2.Response(
                result.status_code,
                content=result.content.replace(
                    b"Change 3", b"Improve catalog validation"
                ),
                headers={
                    k: v for k, v in result.headers.items() if k in {"etag", "link"}
                },
            )
        return upstream.respond(request)

    return httpx2.MockTransport(respond)


@pytest.fixture
def embedder() -> CatalogVectors:
    return CatalogVectors()


@pytest.fixture
def chat_model() -> HistoryModel:
    class Model(HistoryModel):
        def invoke(self, messages: list[BaseMessage]) -> object:
            data = messages[-1].content
            assert isinstance(data, str)
            if any(e.get("source") == "history" for e in json.loads(data)["evidence"]):
                return super().invoke(messages)
            return {"claims": []}

    return Model()


def test_mixed_import_refresh_previous_evidence_and_offline_cli(
    page: Page, app_url: str, tmp_path: Path, upstream: Upstream
) -> None:
    module = "export function decode() {\n  return 1;\n}\n"
    files = FILES | {
        "decoder.ts": module,
        "go.mod": "module example/demo\n",
        "reader.go": "package demo\nfunc Read() {}\n",
    }
    upstream.catalog = Catalog(files)
    old = upstream.catalog.sha
    page.goto(app_url)
    page.get_by_label("Public GitHub URL").fill("https://github.com/" + REPO)
    page.get_by_role("button", name="Import repository").click()
    page.get_by_role("link", name="Browse source").click()
    page.get_by_role("link", name="decoder.ts", exact=True).click()
    page.get_by_role("link", name="decode function", exact=True).click()
    expect(page.locator("#L2.selected")).to_contain_text("return 1")
    assert old in page.url
    upstream.catalog = Catalog(
        files
        | {
            "decoder.ts": "// Updated declaration\n"
            + module.replace("return 1", "return 2")
        }
    )
    new = upstream.catalog.sha
    page.get_by_role("button", name="Refresh repository").click()
    expect(page.get_by_text("Refresh complete", exact=False)).to_be_visible()
    status = page.url
    page.get_by_role("link", name=f"Browse previous commit {old[:8]}").click()
    page.get_by_role("link", name="decoder.ts", exact=True).click()
    page.get_by_role("link", name="decode function", exact=True).click()
    assert f"/{old}/code?" in page.url and page.url.endswith("#L1")
    expect(page.locator("#L2.selected")).to_contain_text("return 1")
    page.goto(status)
    page.get_by_role("link", name="Browse source").click()
    page.get_by_role("link", name="decoder.ts", exact=True).click()
    page.get_by_role("link", name="decode function", exact=True).click()
    assert f"/{new}/code?" in page.url and page.url.endswith("#L2")
    expect(page.locator("#L3.selected")).to_contain_text("return 2")
    page.get_by_role("link", name="Architecture", exact=True).click()
    expect(page.get_by_role("region", name="Module roles")).to_be_visible()
    page.locator("summary", has_text="Download report").click()
    with page.expect_download() as triggered:
        page.get_by_role("link", name="JSON report").click()
    file = tmp_path / "web-report.json"
    triggered.value.save_as(file)
    report = json.loads(file.read_bytes())
    assert report["sha"] == new and report["repository"].casefold() == REPO.casefold()
    paths = {
        c["path"]
        for item in report["sections"]["Architecture"]
        for c in item["evidence"]
    }
    assert {"decoder.ts", "reader.go", "src/catalog.py"} <= paths
    assert report["sections"]["Quality audit"] is not None
    cli = shutil.which("repopilot", path=sysconfig.get_path("scripts"))
    assert cli
    result = subprocess.run(
        [
            cli,
            "export",
            report["repository"],
            "--sha",
            new,
            "--data-dir",
            str(tmp_path),
        ],
        cwd=tmp_path,
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert result.stdout == file.read_bytes()
    assert upstream.catalog.issue_requests == 0


def test_issue_load_comparison_recommendation_and_historical_reason(
    page: Page, app_url: str, tmp_path: Path, upstream: Upstream
) -> None:
    old, new = "b" * 40, "a" * 40
    files = {"src/demo.py": "def parse(data):\n    return data\n"}
    manifest_snapshot(tmp_path / "sources", files, repository=HISTORY_REPO, sha=old)
    manifest_snapshot(
        tmp_path / "sources",
        files | {"tests/test_demo.py": "from demo import parse\n"},
        repository=HISTORY_REPO,
        sha=new,
    )
    prefix = f"{app_url}/repos/{HISTORY_REPO}/"
    page.goto(prefix + old + "/issues")
    expect(page.get_by_text("0 issues loaded")).to_be_visible()
    assert upstream.catalog.issue_requests == 0
    page.get_by_role("button", name="Load latest 50 issues").click()
    page.get_by_role("button", name="Assess difficulty").click()
    expect(page.get_by_role("region", name="Assessment comparison")).to_contain_text(
        "First evaluation"
    )
    page.goto(prefix + new + "/issues")
    page.get_by_role("button", name="Re-evaluate difficulty").click()
    comparison = page.get_by_role("region", name="Assessment comparison")
    expect(comparison).to_contain_text("Previous score: 45")
    expect(comparison).to_contain_text("New score: 25")
    for label, sha in [("Previous evidence", old), ("Current evidence", new)]:
        comparison.locator("summary", has_text=label).click()
        expect(
            comparison.get_by_role("link", name="src/demo.py · " + sha[:8])
        ).to_have_attribute(
            "href",
            f"/repos/{HISTORY_REPO}/{sha}/code?path=src%2Fdemo.py&line=1&end=2#L1",
        )
    page.get_by_role("link", name="Repository history").click()
    page.get_by_role("button", name="Load history").click()
    page.get_by_role("link", name="Inspect discussion #3", exact=True).click()
    page.get_by_role("button", name="Load history").click()
    expect(page.get_by_text(REASON, exact=True)).to_be_visible()
    page.get_by_role("link", name="Issues", exact=True).click()
    page.get_by_role("link", name="Find a first issue").click()
    recommendations = page.get_by_role("region", name="Recommended first issues")
    recommendations.get_by_role("link", name="Review #20", exact=False).click()
    expect(recommendations).to_contain_text("High confidence")
    recommendations.get_by_role("link", name="src/demo.py · L1–2", exact=True).click()
    expect(page.locator("#L1:target")).to_contain_text("def parse")
    ask = page.get_by_role("region", name="Ask about this code")
    ask.get_by_label("History reference (optional)").fill("#3")
    ask.get_by_label("Question about this commit").fill(
        "Why was the loader change reverted?"
    )
    ask.get_by_role("button", name="Ask").click()
    expect(ask.get_by_text(REASON, exact=True).first).to_be_visible()
    page.route(
        "https://github.com/**", lambda route: route.fulfill(body="Pinned discussion")
    )
    ask.get_by_role("link", name="Open dated GitHub source").click()
    expect(page).to_have_url(
        f"https://github.com/{HISTORY_REPO}/issues/3#issuecomment-19"
    )
    assert upstream.catalog.issue_requests == 1
