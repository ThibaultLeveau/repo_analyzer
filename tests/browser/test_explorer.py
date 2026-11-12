from playwright.sync_api import Page, expect


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
