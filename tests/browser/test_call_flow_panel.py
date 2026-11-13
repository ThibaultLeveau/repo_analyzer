from pathlib import Path

from playwright.sync_api import Page, expect
from test_call_flow import SOURCE
from test_manifests import manifest_snapshot


def test_trace_from_explorer_and_follow_both_views(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(tmp_path / "sources", {"app.py": SOURCE})
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/code?path=app.py")
    page.get_by_role("link", name="start function", exact=True).click()
    page.get_by_role("link", name="Trace outgoing calls").click()
    panel = page.get_by_role("region", name="Entry point call flow")
    expect(panel.get_by_role("heading", name="Calls from app.start")).to_be_visible()
    saved = page.url
    diagram = panel.get_by_role("group", name="Directed call diagram")
    link = diagram.get_by_role("link", name="app.start to app.right at app.py line 3")
    link.click()
    expect(page.locator("#L3:target")).to_contain_text("right(callback)")
    page.go_back()
    expect(page).to_have_url(saved)
    page.add_style_tag(content=".flow-diagram { display: none; }")
    listed = panel.get_by_role("listitem").filter(has_text="app.right → callback")
    expect(listed).to_contain_text("Unresolved")
    listed.get_by_role("link").press("Enter")
    expect(page).to_have_url(base + "/code?path=app.py&line=7&end=7#L7")
    expect(page.locator("#L7:target")).to_contain_text("callback()")
