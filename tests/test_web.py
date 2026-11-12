from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app


class PageLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in {"href", "src"} and value:
                self.urls.append(value)


def test_installed_shell_serves_local_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "No repositories yet" in response.text
        for landmark in ("<header", "<nav", "<main"):
            assert landmark in response.text
        links = PageLinks()
        links.feed(response.text)
        assert any(url.endswith("/htmx-2.0.11.min.js") for url in links.urls)
        for url in links.urls:
            assert urlsplit(url).hostname in {None, "testserver"}
            assert client.get(url).status_code == 200
        assert client.get("/static/vendor/htmx-LICENSE.txt").is_success


@pytest.mark.parametrize(("path", "title"), [("/", "Overview"), ("/setup", "Local setup")])
def test_navigation_representations(path: str, title: str) -> None:
    with TestClient(create_app()) as client:
        full = client.get(path)
        fragment = client.get(path, headers={"HX-Request": "true"})
        restored = client.get(
            path, headers={"HX-Request": "true", "HX-History-Restore-Request": "true"}
        )
    assert "<!doctype html>" in full.text
    assert restored.text == full.text
    assert "<!doctype html>" not in fragment.text
    for response in (full, fragment, restored):
        assert response.status_code == 200
        assert response.headers["Vary"] == "HX-Request, HX-History-Restore-Request"
        assert f"<title>{title} · RepoPilot</title>" in response.text
        assert 'id="workspace"' in response.text
        assert f"<h1>{title}</h1>" in response.text
