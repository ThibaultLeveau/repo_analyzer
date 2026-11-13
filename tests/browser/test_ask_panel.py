from threading import Event

import pytest
from playwright.sync_api import Page, expect
from test_ask import CitedModel


@pytest.fixture
def chat_model() -> CitedModel:
    model = CitedModel()
    model.wait = Event()
    return model


def test_ask_open_source_and_return_to_saved_answer(
    page: Page, app_url: str, explorer_url: str, chat_model: CitedModel
) -> None:
    page.goto(app_url + explorer_url + "?path=src%2Fdemo.py")
    panel = page.get_by_role("region", name="Ask about this code")
    field = panel.get_by_label("Question about this commit")
    field.fill("What does msg contain?")
    panel.get_by_role("button", name="Ask").click()
    try:
        expect(panel.get_by_text("Searching source…")).to_be_visible()
    finally:
        assert chat_model.wait
        chat_model.wait.set()
    expect(panel.get_by_text("The source stores a value.")).to_be_visible()
    expect(panel.locator("pre")).to_contain_text('msg = "<tag>"')
    saved_url = page.url
    panel.get_by_role("link", name="Open cited lines").click()
    expect(page.locator("#L1:target")).to_contain_text("original")
    expect(page.locator("#L2.selected")).to_contain_text("msg")
    page.go_back()
    expect(page).to_have_url(saved_url)
    expect(panel.get_by_text("The source stores a value.")).to_be_visible()
    assert chat_model.calls == 1
    field.fill("unicorns")
    panel.get_by_role("button", name="Ask").click()
    expect(panel.get_by_role("status")).to_contain_text("Insufficient evidence")
