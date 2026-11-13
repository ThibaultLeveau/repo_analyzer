import httpx2
import pytest
from playwright.sync_api import Page, expect
from test_issues import Clock, Pages


@pytest.fixture
def app_clock() -> Clock:
    return Clock()


@pytest.fixture
def github() -> Pages:
    return Pages()


@pytest.fixture
def import_transport(github: Pages) -> httpx2.MockTransport:
    return httpx2.MockTransport(github)


def test_load_filter_extend_and_retain_cached_issues(
    page: Page, app_url: str, explorer_url: str, app_clock: Clock, github: Pages
) -> None:
    page.goto(app_url + explorer_url)
    page.get_by_role("link", name="Issues", exact=True).click()
    expect(page.get_by_text("0 issues loaded")).to_be_visible()
    assert not github.calls
    page.get_by_role("button", name="Load latest 50 issues").click()
    expect(page.get_by_text("50 issues loaded")).to_be_visible()
    next_page = page.get_by_role("button", name="Load next 50")
    expect(next_page).to_be_disabled()
    page.get_by_label("Filter by label").select_option("bug")
    page.get_by_role("button", name="Filter", exact=True).click()
    page.get_by_role("link", name="#148 Issue 148", exact=True).click()
    detail = page.get_by_role("region", name="Issue details")
    expect(detail).to_contain_text("Details for 148")
    expect(detail.get_by_role("link", name="Open on GitHub")).to_have_attribute(
        "href", "https://github.com/Owner/Demo/issues/148"
    )
    assert github.calls == [1]
    page.get_by_label("Minimum interval").fill("1")
    page.get_by_role("button", name="Save interval").click()
    expect(next_page).to_be_disabled()
    app_clock.now += 3600
    page.reload()
    next_page.click()
    expect(page.get_by_text("All open issues loaded", exact=False)).to_contain_text(
        "100 issues loaded"
    )
    expect(next_page).to_be_disabled()
    app_clock.now += 3600
    github.status = 429
    page.reload()
    page.get_by_role("button", name="Refresh newest 50").click()
    expect(page.get_by_role("alert")).to_contain_text("rate limit")
    expect(detail.get_by_role("heading", name="#149 Issue 149")).to_be_visible()
    assert github.calls == [1, 2, 1]
