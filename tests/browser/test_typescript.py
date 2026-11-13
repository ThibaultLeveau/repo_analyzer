from pathlib import Path

from playwright.sync_api import Page, expect
from test_javascript import FILES
from test_manifests import manifest_snapshot


def test_typescript_symbol_and_scoped_package_evidence(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(tmp_path / "sources", FILES)
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/code?path=src%2Fcore.ts")
    page.get_by_role("link", name="load function", exact=True).click()
    expect(page.locator("#L2:target")).to_contain_text("export function load")
    expect(page.locator("#L4.selected")).to_have_text("4}")
    page.get_by_role("link", name="Dependencies", exact=True).click()
    page.get_by_role("region", name="Packages", exact=True).get_by_role(
        "link", name="npm:@scope/client"
    ).click()
    page.get_by_role("link", name="src/ui.tsx · L2").click()
    expect(page.locator("#L2:target")).to_contain_text("@scope/client/http")
