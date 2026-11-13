from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage
from playwright.sync_api import Page, expect
from test_maintenance import SHA, SOURCE
from test_manifests import manifest_snapshot

from repopilot.grounding import StructuredModel


@pytest.fixture
def chat_model() -> StructuredModel:
    class Unavailable:
        def invoke(self, messages: list[BaseMessage]) -> object:
            raise TimeoutError

    return Unavailable()


def test_review_download_after_failed_explanation(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    snap = manifest_snapshot(tmp_path / "sources", {"collect.py": SOURCE})
    base = app_url + "/repos/owner/repo/" + SHA
    page.goto(base + "/audit")
    page.get_by_role("link", name="Mutable default for collect(items)").click()
    page.get_by_role("button", name="Explain observation").click()
    expect(page.get_by_role("status")).to_contain_text("Model explanation unavailable")
    page.get_by_role("link", name="Preview maintenance fix").click()
    panel = page.get_by_role("region", name="Patch preview")
    expect(panel).to_contain_text("+    if items is None:")
    expect(panel.get_by_role("link", name="collect.py · L1–1")).to_have_attribute(
        "href", base + "/code?path=collect.py&line=1&end=1#L1"
    )
    with page.expect_download() as trigger:
        panel.get_by_role("link", name="Download patch").click()
    assert trigger.value.suggested_filename == "maintenance.patch"
    save = tmp_path / "maintenance.patch"
    trigger.value.save_as(save)
    assert "@@ -1,4 +1,6 @@" in save.read_text("utf-8")
    assert page.request.get(page.url.replace(SHA, "b" * 40)).status == 404
    assert snap.read_text("collect.py") == SOURCE
