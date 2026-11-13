from pathlib import Path

import httpx2
from playwright.sync_api import Page, expect
from test_history import Remote
from test_issues import row
from test_manifests import manifest_snapshot
from test_recommendations import FILES, REPO, SHA

from repopilot.history import History
from repopilot.issues import IssuePages


def test_recommendation_links_to_real_precedent_and_pinned_test(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(tmp_path / "sources", FILES)
    remote = Remote()

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.params.get("state") == "open":
            return httpx2.Response(
                200,
                json=[
                    row(20) | {"title": "Improve catalog validation in src/loader.py"}
                ],
            )
        changed = httpx2.Request(
            "GET",
            str(request.url).replace("/owner/repo", "/owner/demo"),
            headers=request.headers,
        )
        result = remote.respond(changed)
        data = result.text.replace("owner/demo", REPO).replace(
            "Change 3", "Improve catalog validation"
        )
        return httpx2.Response(
            result.status_code,
            content=data.encode(),
            headers={k: v for k, v in result.headers.items() if k in {"etag", "link"}},
        )

    with httpx2.Client(transport=httpx2.MockTransport(respond)) as client:
        IssuePages(tmp_path, client).load(REPO)
        History(tmp_path, client).load(REPO, SHA, 3)
    page.goto(f"{app_url}/repos/{REPO}/{SHA}/issues")
    page.get_by_role("link", name="Find a first issue").click()
    section = page.get_by_role("region", name="Recommended first issues")
    section.get_by_role("link", name="Review #20", exact=False).click()
    page.wait_for_url("**/issues?number=20&recommendations=true#recommendation-20")
    expect(section.get_by_text("High confidence.", exact=False)).to_be_visible()
    saved = page.url
    page.route(
        "https://github.com/**", lambda route: route.fulfill(body="Closed precedent")
    )
    section.get_by_role("link", name="#3 Improve catalog validation").click()
    expect(page).to_have_url("https://github.com/owner/repo/issues/3")
    page.go_back()
    expect(page).to_have_url(saved)
    section.get_by_role("link", name="tests/test_loader.py", exact=False).click()
    expect(page.locator("#L1:target")).to_contain_text("from loader import parse")
