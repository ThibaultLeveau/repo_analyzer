from pathlib import Path

from playwright.sync_api import Page, expect


def test_semantic_index_missing_key_keeps_search_available(
    page: Page, app_url: str, explorer_url: str
) -> None:
    page.goto(app_url + explorer_url)
    search = page.get_by_role("searchbox")
    search.press("Enter")
    page.get_by_role("button", name="Build semantic index").click()
    expect(page.get_by_role("status")).to_contain_text("Set OPENAI_API_KEY")
    search.fill("msg")
    search.press("Enter")
    expect(page.get_by_role("link", name="demo.msg")).to_be_visible()


def test_search_by_keyboard_and_follow_symbol(
    page: Page, app_url: str, explorer_url: str
) -> None:
    page.goto(app_url + explorer_url)
    search = page.get_by_role("searchbox", name="Search this snapshot")
    results = page.get_by_role("region", name="Search results")
    search.press("Enter")
    expect(results).to_contain_text("Enter a path")
    search.fill("zero results")
    search.press("Enter")
    expect(results).to_contain_text("No matches")
    search.fill("msg")
    search.press("Enter")
    result = results.get_by_role("link", name="demo.msg")
    expect(result).to_be_visible()
    search.focus()
    for _ in range(12):
        page.keyboard.press("Tab")
        if result.evaluate("element => document.activeElement === element"):
            break
    expect(result).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.locator("#L2:target.selected")).to_contain_text('msg = "<tag>"')
    assert explorer_url in page.url


def test_copy_download_and_follow_setup_evidence(
    page: Page, app_url: str, explorer_url: str, tmp_path: Path
) -> None:
    page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"], origin=app_url
    )
    page.goto(app_url + explorer_url)
    page.get_by_role("link", name="Getting Started").click()
    install = page.get_by_role("region", name="Installation")
    command = "python -m pip install -r requirements.txt"
    expect(install.locator("code")).to_have_text(command)
    expect(install).to_contain_text("Unverified")
    install.get_by_role("button", name="Copy command").click()
    expect(install.get_by_role("button", name="Copied")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()") == command
    with page.expect_download() as event:
        page.get_by_role("link", name="Download Markdown").click()
    file = tmp_path / event.value.suggested_filename
    event.value.save_as(file)
    markdown = file.read_text(encoding="utf-8")
    assert command in markdown and "Unverified" in markdown
    install.get_by_role("link", name="requirements.txt · L1").click()
    expect(page.locator("#L1:target")).to_contain_text("PyYAML>=6")
    assert explorer_url in page.url


def test_select_dependency_and_open_import(
    page: Page, app_url: str, explorer_url: str
) -> None:
    page.goto(app_url + explorer_url)
    page.get_by_role("link", name="Dependencies", exact=True).click()
    packages = page.get_by_role("region", name="Packages", exact=True)
    packages.get_by_role("link", name="pyyaml", exact=True).click()
    expect(packages).to_contain_text("1 importing file")
    panel = page.get_by_role("region", name="Import evidence")
    panel.get_by_role("link", name="tests/check.py · L1").click()
    expect(page.locator("#L1:target")).to_contain_text("import yaml")
    assert explorer_url in page.url


def test_select_module_and_open_its_evidence(
    page: Page, app_url: str, explorer_url: str
) -> None:
    page.goto(app_url + explorer_url)
    page.get_by_role("link", name="Architecture", exact=True).click()
    page.get_by_role("link", name="demo.py", exact=True).click()
    evidence = page.get_by_role("region", name="Module evidence")
    expect(evidence.get_by_role("heading", name="demo")).to_be_visible()
    evidence.get_by_role("link", name="src/demo.py · L2–2").click()
    expect(page.locator("#L2:target")).to_contain_text('msg = "<tag>"')
    assert explorer_url in page.url


def test_follow_pinned_source_line(page: Page, app_url: str, explorer_url: str) -> None:
    page.goto(app_url + explorer_url)
    page.locator("summary", has_text="src").click()
    with page.expect_request(lambda req: req.headers.get("hx-request") == "true"):
        page.get_by_role("link", name="demo.py").click()
    expect(
        page.get_by_role("heading", name="Owner/Demo / src / demo.py")
    ).to_be_visible()
    page.get_by_role("link", name="Line 2", exact=True).click()
    expect(page.locator("#L2:target")).to_contain_text('msg = "<tag>"')
    pinned = page.url
    assert explorer_url in pinned and pinned.endswith("&line=2#L2")
    page.reload()
    expect(page.locator("#L2:target")).to_be_visible()
    page.get_by_role("link", name="README.md").click()
    expect(page.get_by_role("heading", name="Owner/Demo / README.md")).to_be_visible()
    page.go_back()
    expect(page).to_have_url(pinned)
    expect(page.locator("#L2:target")).to_contain_text('msg = "<tag>"')
