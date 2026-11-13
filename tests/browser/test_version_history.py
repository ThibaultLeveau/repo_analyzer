from pathlib import Path

import httpx2
import pytest
from playwright.sync_api import Page, expect
from test_dependency_history import (
    CHANGE,
    EDIT,
    HEAD,
    MANIFEST,
    GoHistory,
    HistoryFixture,
    project,
)
from test_manifests import manifest_snapshot


@pytest.fixture
def history_fixture(request: pytest.FixtureRequest) -> HistoryFixture:
    return (
        GoHistory("change")
        if getattr(request, "param", "") == "go"
        else HistoryFixture("change")
    )


@pytest.fixture
def import_transport(history_fixture: HistoryFixture) -> httpx2.MockTransport:
    return httpx2.MockTransport(history_fixture.handle)


def test_on_demand_commit_link_and_incomplete_lookup(
    page: Page, app_url: str, tmp_path: Path, history_fixture: HistoryFixture
) -> None:
    manifest_snapshot(
        tmp_path / "sources",
        {
            MANIFEST: project("==2")
            + "[project.optional-dependencies]\ndev=['other==2']"
        },
    )
    base = app_url + "/repos/owner/repo/" + HEAD + "/dependencies"
    page.goto(base + "?package=example")
    assert not history_fixture.requests
    page.get_by_role("link", name="Look up version history").click()
    panel = page.get_by_role("region", name="Version history")
    expect(panel).to_contain_text("Declared version changed")
    commit_url = "https://github.com/owner/repo/commit/" + CHANGE
    page.route(commit_url, lambda route: route.fulfill(body="Fixture commit"))
    panel.get_by_role("link", name="Commit " + CHANGE[:8]).click()
    expect(page).to_have_url(commit_url)
    history_fixture.scenario = "shallow"
    page.goto(base + "?package=other")
    page.get_by_role("link", name="Look up version history").click()
    expect(panel).to_contain_text("Unknown")
    expect(panel.get_by_role("link")).to_have_count(0)


@pytest.mark.parametrize("history_fixture", ["go"], indirect=True)
def test_go_requirement_opens_the_commit_and_original_line(
    page: Page, app_url: str, tmp_path: Path, history_fixture: HistoryFixture
) -> None:
    manifest_snapshot(
        tmp_path / "sources", {history_fixture.path: history_fixture.content(EDIT)}
    )
    page.goto(
        f"{app_url}/repos/owner/repo/{HEAD}/dependencies?package=go%3Aexample.org%2Flib%2Fv2"
    )
    page.get_by_role("link", name="Look up version history").click()
    panel = page.get_by_role("region", name="Version history")
    expect(panel).to_contain_text("Declared version changed")
    expect(panel).to_contain_text(GoHistory.version)
    expect(panel).to_contain_text("2025-01-02")
    url = f"https://github.com/owner/repo/blob/{CHANGE}/service/go.mod"
    page.route(url, lambda r: r.fulfill(body="Observed Go manifest"))
    panel.get_by_role("link", name="service/go.mod · L2").click()
    expect(page).to_have_url(url + "#L2")
