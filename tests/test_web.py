import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.state import StateStore


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
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "No repositories yet" in response.text
        for landmark in ("<header", "<nav", "<main"):
            assert landmark in response.text
        links = PageLinks()
        links.feed(response.text)
        assert any(url.endswith("/htmx-2.0.11.min.js") for url in links.urls)
        for url in links.urls:
            assert urlsplit(url).hostname in {None, "localhost"}
            assert client.get(url).status_code == 200
        assert client.get("/static/vendor/htmx-LICENSE.txt").is_success


@pytest.mark.parametrize(
    ("path", "title"), [("/", "Overview"), ("/setup", "Local setup")]
)
def test_navigation_representations(path: str, title: str, tmp_path: Path) -> None:
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
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


def test_protected_forms_and_read_only_status(tmp_path: Path) -> None:
    def unexpected(request: httpx2.Request) -> httpx2.Response:
        pytest.fail("A read or rejected submission contacted GitHub")

    app = create_app(data_dir=tmp_path, transport=httpx2.MockTransport(unexpected))
    with TestClient(app, base_url="http://localhost") as client:
        overview = client.get("/")
        token = re.search(r'name="csrf_token" value="([^"]+)"', overview.text)
        assert token
        data = {"csrf_token": token[1], "url": "https://github.com/owner/repo"}
        assert client.get("/", headers={"Host": "foreign.example"}).status_code == 400
        assert (
            client.post(
                "/imports", data=data, headers={"Origin": "https://foreign.example"}
            ).status_code
            == 403
        )
        assert (
            client.post("/imports", data=data | {"csrf_token": "invalid"}).status_code
            == 403
        )
        assert client.post("/imports", data={"url": data["url"]}).status_code == 422
        invalid = client.post(
            "/imports", data=data | {"url": "https://example.com/a/b"}
        )
        assert (
            invalid.status_code == 422 and "public https://github.com" in invalid.text
        )
        store = StateStore(tmp_path / "state.db")
        assert store.recent_imports() == []
        job, _ = store.submit("owner/repo")
        url = f"/imports/{job.id}"
        assert "Queued" in client.get(url).text
        store.start(job.id)
        fragment = client.get(url, headers={"HX-Request": "true"})
        assert (
            "Resolving repository" in fragment.text and "<!doctype" not in fragment.text
        )
        assert client.post(url + "/retry", data=data).status_code == 409
        assert (
            client.post(url + "/retry", data={"csrf_token": "invalid"}).status_code
            == 403
        )
        assert client.get("/imports/999").status_code == 404
        assert len(store.recent_imports()) == 1
