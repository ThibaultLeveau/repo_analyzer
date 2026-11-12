import pytest
from playwright.sync_api import Page, expect


@pytest.mark.parametrize("width", [1280, 360], ids=["desktop", "mobile"])
def test_workspace_navigation(page: Page, app_url: str, width: int) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    page.goto(app_url)
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="Skip to content")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("main")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="View local setup")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Development server")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.go_back()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
