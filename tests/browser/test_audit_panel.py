from pathlib import Path

from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot


def test_inspect_quality_finding_and_follow_pinned_annotation(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(
        tmp_path / "sources", {"src/tools.py": "# actions\ndef Write():\n    pass\n"}
    )
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/architecture")
    quality = page.get_by_role("region", name="Quality", exact=True)
    expect(quality).to_contain_text("2 static findings")
    quality.get_by_role("link", name="Open quality audit").click()
    page.get_by_role("link", name="Function name Write", exact=True).click()
    panel = page.get_by_role("region", name="Annotated source")
    expect(panel).to_contain_text("STYLE001")
    expect(panel.locator("code")).to_contain_text("2  def Write():")
    expect(page.get_by_text("Line coverage is unknown.", exact=False)).to_be_visible()
    panel.get_by_role("link", name="src/tools.py · L2–3").click()
    expect(page).to_have_url(base + "/code?path=src%2Ftools.py&line=2&end=3#L2")
    expect(page.locator("#L2:target")).to_contain_text("def Write")


def test_empty_quality_audit_keeps_execution_status_unknown(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(
        tmp_path / "sources",
        {"utils.py": "def read(): pass", "tests/test_utils.py": ""},
    )
    page.goto(app_url + "/repos/owner/repo/" + "a" * 40 + "/audit")
    expect(page.get_by_role("region", name="Static findings")).to_contain_text(
        "No findings"
    )
    expect(
        page.get_by_text("1 conventionally named test file.", exact=False)
    ).to_contain_text("Test execution status is unknown")
