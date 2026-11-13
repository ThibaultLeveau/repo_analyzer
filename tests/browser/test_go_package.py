from pathlib import Path

from playwright.sync_api import Page, expect
from test_golang import FILES, MOD
from test_manifests import manifest_snapshot


def test_follow_go_package_to_receiver_and_source(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(tmp_path / "sources", FILES)
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/architecture")
    page.get_by_role("complementary", name="Package tree").get_by_role(
        "link", name="reader.go", exact=True
    ).click()
    evidence = page.get_by_role("region", name="Module evidence")
    expect(
        evidence.get_by_role("heading", name=MOD + "/data", exact=True)
    ).to_be_visible()
    evidence.get_by_role("link").first.click()
    page.get_by_role("link", name="Reader.Read method").click()
    expect(page.locator("#L4:target")).to_contain_text("func (r *Reader[T]) Read()")
    expect(page.locator("#L6.selected")).to_be_visible()
    assert "a" * 40 in page.url
    page.goto(base + "/code?path=src%2Fpython.py")
    page.get_by_role("link", name="greet function", exact=True).click()
    expect(page.locator("#L1:target")).to_contain_text("def greet()")
