from pathlib import Path

import httpx2
import pytest
from playwright.sync_api import Page, expect
from test_ask import CitedModel
from test_issues import row
from test_manifests import manifest_snapshot


@pytest.fixture
def chat_model() -> CitedModel:
    return CitedModel()


@pytest.fixture
def import_transport() -> httpx2.MockTransport:
    return httpx2.MockTransport(
        lambda _: httpx2.Response(
            200, json=[row(2) | {"title": "Change src/core.py"}, row(1)]
        )
    )


def test_explicit_issue_assessment_and_component_filter(
    page: Page, app_url: str, tmp_path: Path, chat_model: CitedModel
) -> None:
    manifest_snapshot(
        tmp_path / "sources",
        {"src/core.py": "def run(): pass", "tests/test_core.py": "import core"},
    )
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/issues")
    page.get_by_role("button", name="Load latest 50 issues").click()
    expect(page.get_by_text("Unscored", exact=True)).to_have_count(2)
    assert chat_model.calls == 0
    page.get_by_role("button", name="Assess difficulty").click()
    panel = page.get_by_role("region", name="Issue assessment")
    expect(panel).to_contain_text("Low · 25/100")
    expect(panel).to_contain_text("The source stores a value.")
    page.get_by_label("Filter by component").select_option("core")
    page.get_by_role("button", name="Filter", exact=True).click()
    expect(page.get_by_text("Unscored", exact=True)).to_have_count(0)
    panel.get_by_role("link", name="src/core.py · L1–1").click()
    expect(page).to_have_url(base + "/code?path=src%2Fcore.py&line=1&end=1#L1")
    assert chat_model.calls == 1
