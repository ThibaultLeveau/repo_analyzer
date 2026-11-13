import httpx2
import pytest
from journey_fixture import FILES, REPO, Catalog
from playwright.sync_api import Page, expect
from test_refresh import Upstream


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


@pytest.fixture
def import_transport(upstream: Upstream) -> httpx2.MockTransport:
    return httpx2.MockTransport(upstream.respond)


def test_refresh_failure_then_retry_keeps_previous_source(
    page: Page, app_url: str, upstream: Upstream
) -> None:
    page.goto(app_url)
    page.get_by_label("Public GitHub URL").fill(f"https://github.com/{REPO}")
    page.get_by_role("button", name="Import repository").click()
    page.get_by_role("link", name="Browse source").click()
    page.wait_for_url("**/code")
    previous_url = page.url
    previous_sha = upstream.catalog.sha
    token = page.locator('input[name="csrf_token"]').first.input_value()
    upstream.catalog = Catalog(FILES | {"marker.txt": "Updated."})
    upstream.block = True
    try:
        page.get_by_role("button", name="Refresh repository").click()
        assert upstream.started.wait(10)
        duplicate = page.request.post(
            previous_url.removesuffix("/code") + "/refresh",
            form={"csrf_token": token},
            max_redirects=0,
        )
        assert duplicate.status == 303 and duplicate.headers["location"] == page.url
        expect(
            page.get_by_role("link", name=f"Browse previous commit {previous_sha[:8]}")
        ).to_have_attribute("href", previous_url)
    finally:
        upstream.release.set()
    expect(page.get_by_role("alert")).to_be_visible()
    upstream.block = False
    page.get_by_role("button", name="Retry import").click()
    expect(page.get_by_text("Refresh complete", exact=False)).to_be_visible()
    page.get_by_role("link", name="Browse source").click()
    expect(page.get_by_role("link", name="marker.txt")).to_be_visible()
    assert upstream.catalog.sha in page.url
    page.goto(previous_url)
    expect(page.get_by_role("link", name="README.md")).to_be_visible()
    expect(page.get_by_role("link", name="marker.txt")).to_have_count(0)
    assert upstream.catalog.issue_requests == 0
