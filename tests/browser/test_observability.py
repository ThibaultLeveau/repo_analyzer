from pathlib import Path
from time import time

from playwright.sync_api import Page, expect

from repopilot.events import EventStore, measure
from repopilot.state import StateStore


def test_inspect_failed_job_and_window_by_keyboard(
    page: Page, app_url: str, explorer_url: str, tmp_path: Path
) -> None:
    jobs = StateStore(tmp_path / "state.db")
    failed, _ = jobs.submit("Owner/Demo")
    jobs.fail(failed.id, "private fixture issue or source text")
    other, _ = jobs.submit("Other/Repository")
    jobs.fail(other.id, "unrelated failure")
    store = EventStore(tmp_path, clock=lambda: time() - 7200)
    with (
        store.scope("Owner/Demo", "a" * 40, failed.id),
        measure("import.fetch") as metrics,
    ):
        metrics.success = False
    page.goto(app_url + explorer_url)
    page.get_by_role("link", name="Observability").click()
    first = page.locator("summary", has_text=f"Job {failed.id} · failed")
    first.click()
    expect(page.get_by_text("No recorded stages in this window.")).to_be_visible()
    select = page.get_by_label("Time window")
    select.focus()
    select.press("ArrowDown")
    select.press("Tab")
    expect(page.get_by_role("button", name="Update metrics")).to_be_focused()
    page.keyboard.press("Enter")
    expect(page).to_have_url(
        app_url + explorer_url.removesuffix("/code") + "/observability?window=24h"
    )
    summary = page.locator("summary", has_text=f"Job {failed.id} · failed")
    summary.focus()
    summary.press("Enter")
    expect(page.get_by_text("import.fetch · Failed", exact=False)).to_be_visible()
    expect(page.get_by_text("Reported tokens")).to_be_visible()
    assert (
        "private fixture" not in page.content()
        and f"Job {other.id} ·" not in page.content()
    )
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
