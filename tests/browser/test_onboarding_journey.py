import httpx2
import pytest
from journey_fixture import REPO, Catalog, CatalogChat, CatalogVectors
from playwright.sync_api import Page, expect


@pytest.fixture
def catalog() -> Catalog:
    return Catalog()


@pytest.fixture
def import_transport(catalog: Catalog) -> httpx2.MockTransport:
    return httpx2.MockTransport(catalog.respond)


@pytest.fixture
def chat_model() -> CatalogChat:
    return CatalogChat()


@pytest.fixture
def embedder() -> CatalogVectors:
    return CatalogVectors()


def test_import_to_cited_answer_and_issue_queue(
    page: Page, app_url: str, catalog: Catalog
) -> None:
    page.goto(app_url)
    page.get_by_label("Public GitHub URL").fill(f"https://github.com/{REPO}")
    page.get_by_role("button", name="Import repository").click()
    page.get_by_role("link", name="Browse source").click()
    page.get_by_text("src", exact=True).click()
    page.get_by_role("link", name="catalog.py", exact=True).click()
    expect(page.locator("#L5")).to_contain_text("yaml.safe_load")
    page.get_by_role("link", name="Architecture", exact=True).click()
    expect(page.get_by_role("region", name="Module roles")).to_contain_text(
        "Read the catalog configuration."
    )
    page.get_by_role("link", name="Dependencies", exact=True).click()
    imports = page.get_by_role("region", name="Import evidence")
    imports.get_by_role("link", name="src/catalog.py · L2").click()
    expect(page.locator("#L2:target")).to_contain_text("import yaml")
    page.get_by_role("link", name="Getting Started", exact=True).click()
    install = page.get_by_role("region", name="Installation")
    expect(install.locator("code")).to_have_text(
        ["python -m pip install -r requirements.txt"] * 2
    )
    expect(install).to_contain_text("Unverified")
    page.get_by_role("searchbox").press("Enter")
    page.get_by_role("button", name="Build semantic index").click()
    expect(page.get_by_role("status")).to_have_text(
        "Semantic index ready with 4 source chunks."
    )
    question = "How does load_catalog parse the configuration?"
    search = page.get_by_role("searchbox")
    search.fill(question)
    search.press("Enter")
    first = (
        page.get_by_role("region", name="Search results").get_by_role("listitem").first
    )
    expect(first).to_contain_text("Lexical + Semantic")
    first.get_by_role("link").click()
    expect(page.locator("#L5.selected")).to_contain_text("yaml.safe_load")
    source_url = page.url
    panel = page.get_by_role("region", name="Ask about this code")
    panel.get_by_label("Question about this commit").fill(question)
    panel.get_by_role("button", name="Ask").click()
    expect(panel).to_contain_text("It parses the configuration with yaml.safe_load.")
    panel.get_by_role("link", name="Open cited lines").click()
    expect(page).to_have_url(source_url)
    expect(page.locator("#L5.selected")).to_contain_text("yaml.safe_load")
    assert f"/{catalog.sha}/code?path=src%2Fcatalog.py&line=1&end=5" in page.url
    page.get_by_role("link", name="Issues", exact=True).click()
    expect(page.get_by_text("0 issues loaded")).to_be_visible()
    assert catalog.issue_requests == 0
    page.get_by_role("button", name="Load latest 50 issues").click()
    details = page.get_by_role("region", name="Issue details")
    expect(
        details.get_by_role("heading", name="#7 Document the catalog format")
    ).to_be_visible()
    expect(details.get_by_role("link", name="Open on GitHub")).to_have_attribute(
        "href", f"https://github.com/{REPO}/issues/7"
    )
    assert catalog.issue_requests == 1
