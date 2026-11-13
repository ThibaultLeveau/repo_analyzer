from pathlib import Path

import httpx2
import pytest
from playwright.sync_api import Page, expect
from test_issues import row
from test_manifests import manifest_snapshot

from repopilot.issues import IssuePages
from repopilot.models import RepositoryHead
from repopilot.state import StateStore
from repopilot.vectors import VectorIndex


@pytest.fixture
def import_transport() -> httpx2.MockTransport:
    def offline(request: httpx2.Request) -> httpx2.Response:
        pytest.fail(f"Workspace navigation contacted {request.url}")

    return httpx2.MockTransport(offline)


def test_switch_search_back_and_mobile(
    page: Page, app_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_embeddings(*args: object, **kwargs: object) -> None:
        pytest.fail("Workspace search requested a query embedding")

    monkeypatch.setattr(VectorIndex, "nearest", no_embeddings)
    store = StateStore(tmp_path / "state.db")
    for repo, sha, text in (
        ("owner/one", "a" * 40, "first"),
        ("owner/two", "b" * 40, "second"),
        ("owner/two", "c" * 40, "previous"),
    ):
        manifest_snapshot(
            tmp_path / "sources",
            {"shared.py": f"def shared(): return '{text}'"},
            repository=repo,
            sha=sha,
        )
        job, _ = store.submit(repo)
        store.start(job.id)
        store.pin(job.id, RepositoryHead(repo, "main", sha))
        store.complete(job.id, sha)

    def responder(request: httpx2.Request) -> httpx2.Response:
        name = request.url.path.split("/")[-2]
        return httpx2.Response(200, json=[row(7) | {"title": "Shared task " + name}])

    with httpx2.Client(transport=httpx2.MockTransport(responder)) as client:
        queue = IssuePages(tmp_path, client)
        queue.load("owner/one")
        queue.load("owner/two")
    base = app_url + "/repos/owner/two/" + "b" * 40
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(app_url + "/repos/owner/one/" + "a" * 40 + "/code")
    switch = page.get_by_label("Switch repository or snapshot")
    switch.focus()
    page.keyboard.press("Enter")
    page.get_by_role("link", name="owner/two · main · bbbbbbbbbbbb").click()
    expect(page).to_have_url(base + "/architecture")
    expect(switch).to_contain_text("bbbbbbbb")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.keyboard.press("Control+k")
    field = page.get_by_role("searchbox")
    expect(field).to_be_focused()
    field.fill("shared")
    field.press("Enter")
    files = page.get_by_role("region", name="Files", exact=True)
    expect(files.get_by_role("link", name="shared.py")).to_be_visible()
    issues = page.get_by_role("region", name="Issues", exact=True)
    expect(issues).to_contain_text("Shared task two")
    expect(issues).not_to_contain_text("Shared task one")
    results_url = page.url
    page.get_by_role("region", name="Symbols").get_by_role(
        "link", name="shared.shared"
    ).click()
    expect(page.locator("#L1:target")).to_contain_text("'second'")
    page.go_back()
    expect(page).to_have_url(results_url)
    issues.get_by_role("link").click()
    expect(page).to_have_url(base + "/issues?number=7")
    page.go_back()
    field.fill("ccccccc")
    field.press("Enter")
    page.get_by_role("region", name="Commits").get_by_role("link").click()
    expect(page).to_have_url(base.replace("b" * 40, "c" * 40) + "/architecture")
    field.fill("missing-result-xyz")
    field.press("Enter")
    expect(page.get_by_role("region", name="Search results")).to_contain_text(
        "No matches"
    )
