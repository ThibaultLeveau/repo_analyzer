import json
from collections.abc import Iterable
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect
from test_manifests import manifest_snapshot

from repopilot import specialists
from repopilot.audit import Finding
from repopilot.events import EventStore


def test_inspect_quality_finding_and_follow_pinned_annotation(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(
        tmp_path / "sources", {"src/tools.py": "# actions\ndef Write():\n    pass\n"}
    )
    base = app_url + "/repos/owner/repo/" + "a" * 40
    page.goto(base + "/architecture")
    quality = page.get_by_role("region", name="Quality", exact=True)
    expect(quality).to_contain_text("2 static findings")
    quality.get_by_role("link", name="Open quality audit").click()
    page.get_by_role("link", name="Function name Write", exact=True).click()
    panel = page.get_by_role("region", name="Annotated source")
    expect(panel).to_contain_text("STYLE001")
    expect(panel.locator("code")).to_contain_text("2  def Write():")
    expect(page.get_by_text("Line coverage is unknown.", exact=False)).to_be_visible()
    panel.get_by_role("link", name="src/tools.py · L2–3").click()
    expect(page).to_have_url(base + "/code?path=src%2Ftools.py&line=2&end=3#L2")
    expect(page.locator("#L2:target")).to_contain_text("def Write")


def test_empty_quality_audit_keeps_execution_status_unknown(
    page: Page, app_url: str, tmp_path: Path
) -> None:
    manifest_snapshot(
        tmp_path / "sources",
        {"utils.py": "def read(): pass", "tests/test_utils.py": ""},
    )
    page.goto(app_url + "/repos/owner/repo/" + "a" * 40 + "/audit")
    expect(page.get_by_role("region", name="Static findings")).to_contain_text(
        "No findings"
    )
    expect(
        page.get_by_text("1 conventionally named test file.", exact=False)
    ).to_contain_text("Test execution status is unknown")


def test_partial_audit_filter_export_and_retry(
    page: Page, app_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "import logging, re\nlogging.info('private fixture', token)\nfor x in xs:\n    re.compile('fixture')\n"
    manifest_snapshot(tmp_path / "sources", {"main.py": source})
    base = app_url + "/repos/owner/repo/" + "a" * 40

    def unavailable(parsed: Iterable[object]) -> list[Finding]:
        raise RuntimeError("Internal diagnostic")

    with monkeypatch.context() as patch:
        patch.setattr(specialists, "performance", unavailable)
        page.goto(base + "/audit")
        page.get_by_role("navigation", name="Finding categories").get_by_role(
            "link", name="Security"
        ).click()
        expect(page.get_by_text("Partial audit.", exact=False)).to_contain_text(
            "performance"
        )
        panel = page.get_by_role("region", name="Annotated source")
        title = panel.get_by_role("heading").inner_text()
        expect(panel).to_contain_text("SEC001")
        expect(panel).not_to_contain_text("private fixture")
        panel.get_by_role("button", name="Explain observation").click()
        expect(page.get_by_text("Model explanation unavailable.")).to_be_visible()
        page.locator("summary", has_text="Download report").click()
        with page.expect_download() as trigger:
            page.get_by_role("link", name="JSON report").click()
        file = tmp_path / "audit.json"
        trigger.value.save_as(file)
        sections = json.loads(file.read_text("utf-8"))["sections"]
        entry = next(e for e in sections["Quality audit"] if e["title"] == title)
        assert (entry["category"], entry["severity"], entry["rule"]) == (
            "security",
            "warning",
            "SEC001",
        )
        assert entry["evidence"][0]["start"] == entry["evidence"][0]["end"] == 2
        assert "performance" in sections["Audit status"][0]["text"]
        for label in ("Markdown", "HTML"):
            url = page.get_by_role("link", name=label + " report").get_attribute("href")
            assert url
            contents = page.request.get(url).text()
            assert (
                title in contents
                and "SEC001" in contents
                and "Partial audit" in contents
            )
        panel.get_by_role("link", name="main.py · L2–2").click()
        expect(page.locator("#L2:target")).to_contain_text("logging.info")

    page.goto(base + "/audit?category=performance")
    expect(page.get_by_text("Partial audit.", exact=False)).to_have_count(0)
    expect(page.get_by_role("region", name="Annotated source")).to_contain_text(
        "PERF001"
    )
    events = EventStore(tmp_path).recent("owner/repo")
    assert {
        e.metrics.success for e in events if e.operation == "audit.performance"
    } == {True, False}
    assert any(e.metrics.success and e.operation == "audit.security" for e in events)
