from pathlib import Path

from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot


def test_follow_import_with_the_keyboard_and_open_pinned_source(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    files = {
        "a.py": "# bound to the snapshot\nfrom b import run\n",
        "b.py": "def run(): pass",
    }
    manifest_snapshot(tmp_path / "sources", files)
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/architecture")
    tree = page.get_by_role("complementary", name="Package tree")
    tree.get_by_role("link", name="a.py").press("Enter")
    panel = page.get_by_role("region", name="Module relationships")
    expect(panel.get_by_role("img")).to_be_visible()
    page.add_style_tag(content=".graph-canvas { display: none; }")
    expect(panel.get_by_role("listitem")).to_contain_text("a imports b")
    panel.get_by_role("link", name="b", exact=True).press("Enter")
    expect(
        page.get_by_role("region", name="Module evidence").get_by_role(
            "heading", name="b", exact=True
        )
    ).to_be_visible()
    panel.get_by_role("link", name="a.py · L2", exact=True).press("Enter")
    expect(page).to_have_url(base + "/code?path=a.py&line=2&end=2#L2")
    expect(page.locator("#L2:target")).to_contain_text("from b import run")
