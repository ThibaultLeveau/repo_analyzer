from pathlib import Path

from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot

from repopilot.user_notes import Notes


def test_drop_preview_replace_and_remove(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(tmp_path / "sources", {"README.md": "Start here."})
    base = app_url + "/repos/owner/repo/" + "a" * 40 + "/getting-started"
    page.goto(base)
    panel = page.get_by_role("region", name="Project notes")
    panel.locator("form").first.evaluate(r"""form => {
        const dt = new DataTransfer();
        dt.items.add(new File(["# Queue\n\nUse a walnut. <script>hiddenMarker()</script>"], "notes.md", {type: "text/markdown"}));
        form.dispatchEvent(new DragEvent("drop", {dataTransfer: dt, bubbles: true, cancelable: true}));
    }""")
    panel.get_by_role("button", name="Add note").click()
    preview = panel.get_by_role("article", name="User note preview")
    expect(preview.get_by_role("heading", name="Queue")).to_be_visible()
    expect(preview).to_contain_text("<script>hiddenMarker()</script>")
    assert preview.locator("script").count() == 0
    previous = page.url.split("#")[0] + "#heading-1"
    panel.get_by_label("Replacement Markdown file").set_input_files(
        {
            "name": "notes.md",
            "mimeType": "text/plain",
            "buffer": b"# Process\nUse chestnut.",
        }
    )
    panel.get_by_role("button", name="Replace note").click()
    expect(preview.get_by_role("heading", name="Process")).to_be_visible()
    current = page.url
    store = Notes(tmp_path)
    assert not store.search("owner/repo", "walnut")
    assert store.search("owner/repo", "chestnut")[0][0].filename == "notes.md"
    page.goto(previous)
    expect(page.locator(":target")).to_have_text("Queue")
    expect(preview).to_contain_text("Previous revision")
    page.goto(current)
    panel.get_by_role("button", name="Remove note").click()
    expect(preview).to_have_count(0)
    assert page.request.get(previous).status == 404
    assert not store.search("owner/repo", "chestnut")
