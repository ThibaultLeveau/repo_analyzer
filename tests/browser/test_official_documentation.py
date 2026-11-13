from collections.abc import Callable
from pathlib import Path

import httpx2
import pytest
from journey_fixture import REPO, Catalog
from playwright.sync_api import Page, expect
from test_official_docs import HTML, Publisher

from repopilot.official_docs import DEFAULTS, OfficialDocs


@pytest.fixture
def publisher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Publisher:
    remote = Publisher(tmp_path)

    def docs(directory: Path, clock: Callable[[], float]) -> OfficialDocs:
        return remote.docs

    monkeypatch.setattr("repopilot.app.OfficialDocs", docs)
    return remote


@pytest.fixture
def import_transport(publisher: Publisher) -> httpx2.MockTransport:
    return httpx2.MockTransport(Catalog().respond)


def test_import_select_index_and_open_publisher_section(
    page: Page, app_url: str, publisher: Publisher
) -> None:
    page.goto(app_url)
    page.get_by_label("Public GitHub URL").fill(f"https://github.com/{REPO}")
    page.get_by_role("button", name="Import repository").click()
    page.get_by_role("link", name="View dependencies").click()
    page.get_by_role("region", name="Packages", exact=True).get_by_role(
        "link", name="pyyaml"
    ).click()
    page.get_by_role("link", name="Official documentation").click()
    panel = page.get_by_role("region", name="Official documentation")
    expect(panel).to_contain_text("Unversioned")
    assert not publisher.paths
    panel.get_by_role("button", name="Index documentation").click()
    expect(panel).to_contain_text("Indexed")
    expect(panel).to_contain_text("Declared ==6.0.3")
    panel.get_by_text("Parsing", exact=True).click()
    url = DEFAULTS["pyyaml"].url
    page.route(url, lambda route: route.fulfill(content_type="text/html", body=HTML))
    panel.get_by_role("link", name="Open Parsing").click()
    expect(page).to_have_url(url + "#parsing")
    expect(page.locator(":target")).to_have_text("Parsing")
    assert publisher.paths == ["/robots.txt", "/wiki/PyYAMLDocumentation"]
