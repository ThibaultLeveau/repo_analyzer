import time
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot
from test_official_docs import HTML, Publisher
from test_routing import MIXED, REPO, SHA, SourcesModel

from repopilot.dependencies import analyze_dependencies
from repopilot.official_docs import DEFAULTS


@pytest.fixture
def chat_model() -> SourcesModel:
    return SourcesModel()


def test_read_code_and_external_guidance_with_distinct_citations(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    snapshot = manifest_snapshot(
        tmp_path / "sources",
        {
            "requirements.txt": "PyYAML==6.0.1",
            "parse.py": "def frobnicator():\n    return 42\n",
        },
    )
    remote = Publisher(tmp_path)
    remote.now = time.time()
    remote.docs.index(REPO, SHA, analyze_dependencies(snapshot).packages["pyyaml"])
    page.goto(f"{app_url}/repos/{REPO}/{SHA}/code")
    panel = page.get_by_role("region", name="Ask about this code")
    panel.get_by_label("Question about this commit").fill(MIXED)
    panel.get_by_role("button", name="Ask").click()
    expect(panel.get_by_role("heading", name="Repository observation")).to_be_visible()
    expect(panel.get_by_role("heading", name="Official documentation")).to_be_visible()
    expect(panel).to_contain_text("Declared ==6.0.1")
    saved = page.url
    panel.get_by_role("link", name="Open cited lines").click()
    expect(page.locator("#L1:target")).to_contain_text("frobnicator")
    page.goto(saved)
    url = DEFAULTS["pyyaml"].url
    page.route(url, lambda r: r.fulfill(content_type="text/html", body=HTML))
    panel.get_by_role("link", name="Open official documentation section").click()
    expect(page).to_have_url(url + "#parsing")
    expect(page.locator(":target")).to_have_text("Parsing")
    report = page.request.get(f"{app_url}/repos/{REPO}/{SHA}/report?format=json")
    assert url + "#parsing" in report.text()
