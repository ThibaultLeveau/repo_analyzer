from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot
from test_translation import Translator


@pytest.fixture
def chat_model() -> Translator:
    return Translator()


def test_translate_compare_follow_source_and_recover(
    page: Page, app_url: str, tmp_path: Path, chat_model: Translator
) -> None:
    manifest_snapshot(
        tmp_path / "sources",
        {
            "README.md": "## Prerequisites\nRequires Python and a terminal.\n## Usage\n```sh\npython main.py\n```",
            "main.py": "def dispatch(): pass",
        },
    )
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/getting-started")
    page.get_by_label("Guide language").select_option("fr")
    page.get_by_role("button", name="Show guide").click()
    translated = page.locator('[lang="fr"]')
    expect(translated).to_contain_text("Consulter")
    expect(translated.get_by_text("python main.py", exact=True)).to_be_visible()
    page.get_by_text("Original English guide", exact=True).click()
    original = page.locator("details[open]").filter(
        has=page.get_by_text("Original English guide", exact=True)
    )
    expect(original).to_contain_text("Requires Python")
    expect(original).not_to_contain_text("Consulter")
    saved_url = page.url
    download = page.get_by_role("link", name="Download Markdown").get_attribute("href")
    assert download and "Consulter" in page.request.get(download).text()
    translated.get_by_role("link", name="README.md · L2", exact=True).first.click()
    expect(page.locator("#L2:target")).to_contain_text("Requires Python")
    page.goto(saved_url)
    assert chat_model.calls == 1
    chat_model.invalid = True
    page.get_by_label("Guide language").select_option("es")
    page.get_by_role("button", name="Show guide").click()
    expect(page.get_by_role("status")).to_contain_text("Translation unavailable")
    expect(
        page.get_by_text("Requires Python and a terminal.", exact=True)
    ).to_be_visible()
