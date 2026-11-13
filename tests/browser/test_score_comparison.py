from pathlib import Path

import httpx2
import pytest
from journey_fixture import FILES, REPO, Catalog
from playwright.sync_api import Page, expect

from repopilot.models import RepositoryHead
from repopilot.snapshots import SnapshotCache


@pytest.fixture
def import_transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(Catalog().respond)


def test_compare_two_snapshots_and_open_both_sources(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    cache = SnapshotCache(tmp_path / "sources")
    old = Catalog()
    new = Catalog(
        {path: code for path, code in FILES.items() if not path.startswith("tests/")}
    )
    for fixture in (old, new):
        with httpx2.Client(transport=httpx2.MockTransport(fixture.respond)) as client:
            cache.fetch(RepositoryHead(REPO, "main", fixture.sha), client)
    base = app_url + "/repos/" + REPO
    page.goto(f"{base}/{old.sha}/issues")
    page.get_by_role("button", name="Load latest 50 issues").click()
    page.get_by_role("button", name="Assess difficulty").click()
    panel = page.get_by_role("region", name="Assessment comparison")
    expect(panel).to_contain_text("First evaluation")
    page.goto(f"{base}/{new.sha}/issues")
    page.get_by_role("button", name="Re-evaluate difficulty").click()
    expect(panel).to_contain_text("Previous score: 25")
    expect(panel).to_contain_text("New score: 45")
    expect(panel).to_contain_text("Test-file evidence")
    for label, sha in (("Previous evidence", old.sha), ("Current evidence", new.sha)):
        panel.locator("summary", has_text=label).click()
        link = panel.get_by_role("link", name=f"src/catalog.py · {sha[:8]}")
        link.click()
        expect(page.locator("#L1:target")).to_contain_text(
            "Read the catalog configuration"
        )
        assert sha in page.url
        page.go_back()
    page.get_by_role("button", name="Re-evaluate difficulty").click()
    expect(page.locator("summary", has_text="Assessment history")).to_have_text(
        "Assessment history (2)"
    )
