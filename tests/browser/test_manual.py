import json
import shlex
import subprocess
import sys
from base64 import b64decode
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest
from journey_fixture import Catalog
from playwright.sync_api import Page, expect

ROOT = Path(__file__).resolve().parents[2]


def test_published_manual_and_documented_commands(
    page: Page, tmp_path: Path, cli_env: dict[str, str]
) -> None:
    pytest.importorskip("material")
    target = tmp_path / "site"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "mkdocs",
            "build",
            "--strict",
            "--site-dir",
            str(target),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        timeout=90,
    )
    handler = partial(SimpleHTTPRequestHandler, directory=str(target))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        try:
            page.goto(url)
            expect(
                page.get_by_role("heading", name="Quickstart", exact=True)
            ).to_be_visible()
            expect(page.locator("article pre code").first).to_have_text("python run.py")
            page.locator("article").get_by_role(
                "link", name="repository walkthrough"
            ).click()
            expect(
                page.get_by_role("heading", name="Triage open issues")
            ).to_be_visible()
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.goto(url)
            expect(
                page.get_by_role("heading", name="Quickstart", exact=True)
            ).to_be_visible()
            page.locator("article").get_by_role("link", name="export a report").click()
            expect(
                page.get_by_role("heading", name="Reports and CLI", exact=True)
            ).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            example = page.locator("article pre code").last.inner_text()
            page.set_viewport_size({"width": 1280, "height": 900})
            page.get_by_role(
                "link", name="Technical reference", exact=True
            ).first.click()
            expect(
                page.get_by_role("heading", name="Technical reference", exact=True)
            ).to_be_visible()
            for name, nodes in [
                ("Repository import graph", ("resolve", "fetch", "publish")),
                ("Question and answer graph", ("retrieve", "answer_question", "save")),
            ]:
                graph = page.get_by_role("img", name=name)
                expect(graph).to_be_visible()
                assert graph.evaluate("img => img.complete && img.naturalWidth > 0")
                data = graph.get_attribute("src")
                assert data and data.startswith("data:image/svg+xml;base64,")
                svg = b64decode(data.partition(",")[2]).decode()
                assert all(f">{node}<" in svg for node in nodes)
        finally:
            server.shutdown()
            thread.join(10)
    fixture = Catalog()
    for line in example.strip().splitlines():
        command = shlex.split(line)
        assert command[0] == "repopilot"
        command[2] = command[2].replace(
            "ThibaultLeveau/repo_analyzer", "Example/Catalog"
        )
        command[command.index("--sha") + 1] = fixture.sha
        env = (
            cli_env
            if command[1] == "analyze"
            else cli_env | {"REPOPILOT_FIXTURE_OFFLINE": "1"}
        )
        subprocess.run(
            [sys.executable, "-m", "repopilot.cli", *command[1:]],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            timeout=90,
        )
    assert json.loads((tmp_path / "analysis.json").read_bytes())["sha"] == fixture.sha
    assert fixture.sha in (tmp_path / "saved report.html").read_text(encoding="utf-8")
