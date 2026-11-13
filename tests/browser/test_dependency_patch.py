from pathlib import Path

import httpx2
import pytest
from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot


@pytest.fixture
def import_transport() -> httpx2.MockTransport:
    def offline(request: httpx2.Request) -> httpx2.Response:
        pytest.fail(f"Unexpected external request to {request.url.host}")

    return httpx2.MockTransport(offline)


def test_review_and_download_from_dependency_finding(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    snapshot = manifest_snapshot(
        tmp_path / "sources",
        {"requirements.txt": "# deps\n", "app.py": "from PIL import Image\n"},
    )
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/audit")
    page.get_by_role("link", name="Preview dependency fix").click()
    panel = page.get_by_role("region", name="Patch preview")
    expect(panel).to_contain_text("requirements.txt")
    expect(panel).to_contain_text("+pillow")
    expect(panel.get_by_role("link", name="app.py · L1")).to_have_attribute(
        "href", base + "/code?path=app.py&line=1&end=1#L1"
    )
    with page.expect_download() as event:
        panel.get_by_role("link", name="Download patch").click()
    file = tmp_path / event.value.suggested_filename
    event.value.save_as(file)
    assert b"+++ b/requirements.txt" in file.read_bytes()
    assert snapshot.read_text("requirements.txt") == "# deps\n"
