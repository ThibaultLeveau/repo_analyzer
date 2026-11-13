import json
from pathlib import Path

from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot


def test_download_all_formats_for_current_snapshot(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    sha = "a" * 40
    manifest_snapshot(tmp_path / "sources", {"src/main.py": "def run(): pass"})
    page.goto(app_url + f"/repos/owner/repo/{sha}/architecture")
    page.locator("summary", has_text="Download report").click()
    for label, ext, mime in [
        ("JSON", "json", "application/json"),
        ("Markdown", "md", "text/markdown"),
        ("HTML", "html", "text/html"),
    ]:
        link = page.get_by_role("link", name=f"{label} report")
        expect(link).to_be_visible()
        url = link.get_attribute("href")
        assert url and page.request.get(url).headers["content-type"].startswith(mime)
        with page.expect_download() as triggered:
            link.click()
        file = tmp_path / triggered.value.suggested_filename
        assert file.name == f"repo-{sha[:8]}-report.{ext}"
        triggered.value.save_as(file)
        text = file.read_text(encoding="utf-8")
        assert sha in text and f"/blob/{sha}/src/main.py#L1-L1" in text
        if ext == "json":
            assert json.loads(text)["sections"]["Architecture"][0]["title"] == "main"
