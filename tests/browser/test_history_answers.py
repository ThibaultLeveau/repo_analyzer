from pathlib import Path

import httpx2
import pytest
from playwright.sync_api import Page, expect
from test_history import DATE, REPO, SHA
from test_timeline import REASON, Discussion, HistoryModel

from repopilot.history import History


@pytest.fixture
def chat_model() -> HistoryModel:
    return HistoryModel()


def test_follow_historical_and_source_citations(
    page: Page, app_url: str, explorer_url: str, tmp_path: Path
) -> None:
    with httpx2.Client(transport=httpx2.MockTransport(Discussion().respond)) as client:
        History(tmp_path, client).load(REPO, SHA, 3)
    page.goto(app_url + explorer_url)
    panel = page.get_by_role("region", name="Ask about this code")
    panel.get_by_label("History reference (optional)").fill("#3")
    panel.get_by_label("Question about this commit").fill(
        "Why was the loader change reverted?"
    )
    panel.get_by_role("button", name="Ask").click()
    expect(panel.get_by_text(REASON, exact=True).first).to_be_visible()
    expect(panel.get_by_text("Comment 19 · " + DATE)).to_be_visible()
    saved = page.url
    permalink = f"https://github.com/{REPO}/issues/3#issuecomment-19"
    page.route(
        "https://github.com/**",
        lambda route: route.fulfill(body="Historical discussion"),
    )
    panel.get_by_role("link", name="Open dated GitHub source").click()
    expect(page).to_have_url(permalink)
    page.go_back()
    expect(page).to_have_url(saved)
    panel.get_by_role("link", name="Open cited lines").click()
    expect(page.locator("#L1:target")).to_contain_text("original")
    page.go_back()
    report = page.request.get(
        app_url + explorer_url.removesuffix("/code") + "/report"
    ).json()
    entry = report["sections"]["Questions and answers"][0]
    assert any(
        e["url"] == permalink and "2026-01-04" in e["path"] and e["start"] is None
        for e in entry["evidence"]
    )
