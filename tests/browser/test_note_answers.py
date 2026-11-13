import json
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage
from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot

from repopilot.grounding import StructuredModel


@pytest.fixture
def chat_model() -> StructuredModel:
    class SelectNote:
        def invoke(self, messages: list[BaseMessage]) -> object:
            payload = messages[-1].content
            assert isinstance(payload, str)
            candidates = json.loads(payload)["evidence"]
            assert all(x["source"] == "user_note" for x in candidates)
            return {
                "claims": [
                    {"text": "Model paraphrase", "evidence_ids": [candidates[0]["id"]]}
                ]
            }

    return SelectNote()


def test_saved_note_answers_follow_original_revision(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(tmp_path / "sources", {"main.py": "def start(): pass"})
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/getting-started")
    page.get_by_label("Drop or choose a Markdown file").set_input_files(
        {
            "name": "dispatch.md",
            "mimeType": "text/markdown",
            "buffer": b"# Handoff\nDispatch uses a single queue.",
        }
    )
    page.get_by_role("button", name="Add note").click()
    preview = page.get_by_role("article", name="User note preview")
    expect(preview).to_contain_text("single queue")
    first_note = page.url

    def ask() -> str:
        page.goto(base + "/code")
        panel = page.get_by_role("region", name="Ask about this code")
        panel.get_by_label("Question about this commit").fill(
            "What does my note say about dispatch?"
        )
        panel.get_by_role("button", name="Ask").click()
        page.wait_for_url("**/code?**answer_id=*#ask")
        return page.url

    first_answer = ask()
    expect(page.get_by_role("heading", name="User note", exact=True)).to_be_visible()
    page.get_by_role("link", name="Open user note section").click()
    expect(page.locator(":target")).to_have_text("Handoff")
    page.get_by_label("Replacement Markdown file").set_input_files(
        {
            "name": "dispatch.md",
            "mimeType": "text/plain",
            "buffer": b"# Handoff\nDispatch uses two queues.",
        }
    )
    page.get_by_role("button", name="Replace note").click()
    expect(preview).to_contain_text("two queues")
    current = page.url
    assert ask() != first_answer
    answer = page.get_by_role("region", name="Ask about this code")
    expect(answer).to_contain_text("Dispatch uses two queues.")
    expect(answer).not_to_contain_text("single queue")
    page.goto(first_answer)
    expect(answer).to_contain_text("Dispatch uses a single queue.")
    page.get_by_role("link", name="Open user note section").click()
    expect(page).to_have_url(first_note.split("#")[0] + "#heading-1")
    expect(preview).to_contain_text("Previous revision")
    for ext in ("json", "md", "html"):
        rendered = page.request.get(base + "/report?format=" + ext).text()
        assert (
            "User note" in rendered
            and "single queue" in rendered
            and "two queues" in rendered
        )
    page.goto(current)
    page.get_by_role("button", name="Remove note").click()
    ask()
    expect(answer).to_contain_text("No matching current user note")
    page.goto(first_answer)
    expect(answer).to_contain_text("Dispatch uses a single queue.")
    link = page.get_by_role("link", name="Open user note section").get_attribute("href")
    assert link and page.request.get(app_url + link).status == 404
