from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from test_ask import CitedModel
from test_manifests import manifest_snapshot
from test_references import FILES


@pytest.fixture
def chat_model() -> CitedModel:
    return CitedModel()


def test_explain_follow_caller_and_restore_symbol(
    page: Page, app_url: str, tmp_path: Path, chat_model: CitedModel
) -> None:
    manifest_snapshot(tmp_path / "sources", FILES)
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/code?path=src%2Fcore.py")
    page.get_by_role("link", name="load function", exact=True).click()
    panel = page.get_by_role("region", name="Symbol references")
    expect(panel.get_by_role("heading", name="core.load")).to_be_visible()
    expect(panel).to_contain_text("tests/test_loader.py:4")
    panel.get_by_role("button", name="Explain symbol").click()
    explanation = page.get_by_role("region", name="Ask about this code")
    expect(explanation.get_by_text("The source stores a value.")).to_be_visible()
    cite = explanation.get_by_role("link", name="Open cited lines")
    expect(cite).to_have_attribute(
        "href", f"/repos/owner/repo/{'a' * 40}/code?path=src%2Fcore.py&line=1&end=2#L1"
    )
    saved = page.url
    panel.get_by_role("link", name="client.run · src/client.py:6", exact=True).click()
    expect(page.locator("#L6:target")).to_contain_text("parse(1)")
    assert page.url.startswith(base + "/code?")
    page.go_back()
    expect(page).to_have_url(saved)
    expect(panel.get_by_role("heading", name="core.load")).to_be_visible()
    expect(explanation.get_by_text("The source stores a value.")).to_be_visible()
    assert chat_model.calls == 1
