import io
import zipfile
from threading import Event

import httpx2
import pytest
from playwright.sync_api import Page, expect


@pytest.fixture
def download() -> Event:
    return Event()


@pytest.fixture
def import_transport(download: Event) -> httpx2.MockTransport:
    attempts = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        if request.url.host == "codeload.github.com":
            assert download.wait(timeout=10)
            attempts += 1
            if attempts == 1:
                return httpx2.Response(503)
            data = io.BytesIO()
            with zipfile.ZipFile(data, "w") as archive:
                archive.writestr("repo/README.md", "# Repo")
            return httpx2.Response(200, stream=httpx2.ByteStream(data.getvalue()))
        if "/commits/" in request.url.path:
            return httpx2.Response(200, json={"sha": "a" * 40})
        return httpx2.Response(
            200,
            json={
                "private": False,
                "full_name": "Owner/Repo",
                "default_branch": "main",
            },
        )

    return httpx2.MockTransport(respond)


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
    expect(page.get_by_label("Public GitHub URL")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("button", name="Import repository")).to_be_focused()
    page.keyboard.press("Tab")
    expect(page.get_by_role("link", name="View local setup")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name="Development server")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.go_back()
    expect(page.get_by_role("heading", name="Overview")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


def test_import_progress_and_retry(page: Page, app_url: str, download: Event) -> None:
    page.goto(app_url)
    token = page.locator('input[name="csrf_token"]').input_value()
    page.get_by_label("Public GitHub URL").fill("https://github.com/owner/repo")
    page.get_by_role("button", name="Import repository").click()
    try:
        expect(page.get_by_text("Downloading source", exact=True)).to_be_visible()
        duplicate = page.request.post(
            f"{app_url}/imports",
            form={"csrf_token": token, "url": "https://github.com/owner/repo"},
            max_redirects=0,
        )
        assert duplicate.status == 303 and duplicate.headers["location"] == page.url
    finally:
        download.set()
    expect(page.get_by_role("alert")).to_contain_text("archive")
    page.get_by_role("button", name="Retry import").click()
    expect(page.get_by_text("Import complete", exact=False)).to_be_visible()
    expect(page.get_by_role("heading", name="Owner/Repo")).to_be_visible()
    expect(page.locator("code[title]")).to_have_text("aaaaaaaa")
    page.get_by_role("link", name="Overview", exact=True).click()
    expect(page.get_by_role("heading", name="Recent imports")).to_be_visible()
