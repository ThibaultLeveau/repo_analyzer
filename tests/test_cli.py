import json
import os
import subprocess
import sysconfig
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from journey_fixture import FILES, REPO, Catalog

from repopilot.app import create_app


@pytest.fixture
def cli_env(tmp_path: Path) -> dict[str, str]:
    # Install the same offline GitHub transport in the child interpreter.
    (tmp_path / "sitecustomize.py").write_text(
        """import os
import httpx2
from journey_fixture import Catalog, FILES
old = Catalog()
new = Catalog(FILES | {"next.txt": "second revision"})
def respond(request):
    if os.environ.get("REPOPILOT_FIXTURE_OFFLINE"):
        raise AssertionError("Unexpected network request")
    if request.url.path.endswith("/commits/" + old.sha):
        return httpx2.Response(200, json={"sha":old.sha})
    if request.url.host == "codeload.github.com" and old.sha in request.url.path:
        return old.respond(request)
    return new.respond(request)
original = httpx2.Client.__init__
def init(self, *args, **kwargs):
    kwargs["transport"] = httpx2.MockTransport(respond)
    original(self, *args, **kwargs)
httpx2.Client.__init__ = init
""",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("OPENAI_API_KEY", None)
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(Path(__file__).parent)))
    env["REPOPILOT_DATA_DIR"] = str(tmp_path / "analysis cache")
    return env


def run_cli(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[bytes]:
    name = "repopilot.exe" if os.name == "nt" else "repopilot"
    command = Path(sysconfig.get_path("scripts")) / name
    return subprocess.run(
        [str(command), *args], env=env, capture_output=True, timeout=90, check=False
    )


def test_installed_cli_selects_requested_revision_and_exports_offline(
    cli_env: dict[str, str], tmp_path: Path
) -> None:
    url = "https://github.com/" + REPO
    old = Catalog()
    head = Catalog(FILES | {"next.txt": "second revision"})
    current = run_cli(cli_env, "analyze", url)
    assert current.returncode == 0, current.stderr
    assert json.loads(current.stdout)["sha"] == head.sha
    pinned = run_cli(cli_env, "analyze", url, "--sha", old.sha)
    assert pinned.returncode == 0, pinned.stderr
    report = json.loads(pinned.stdout)
    assert report["sha"] == old.sha and report["sections"]["Getting Started"]
    cached = cli_env | {"REPOPILOT_FIXTURE_OFFLINE": "1"}
    target = tmp_path / "prior commit report.html"
    saved = run_cli(
        cached,
        "export",
        REPO,
        "--sha",
        old.sha,
        "--format",
        "html",
        "--output",
        str(target),
    )
    assert saved.returncode == 0 and not saved.stdout, saved.stderr
    with TestClient(
        create_app(data_dir=Path(cli_env["REPOPILOT_DATA_DIR"])),
        base_url="http://localhost",
    ) as client:
        response = client.get(f"/repos/{REPO}/{old.sha}/report?format=html")
    assert response.status_code == 200 and target.read_bytes() == response.content
    provider = run_cli(cli_env, "analyze", url, "--generate-guide")
    assert (
        provider.returncode == 1
        and b"missing_key" in provider.stderr
        and not provider.stdout
    )
    failure = run_cli(
        cached, "export", REPO, "--sha", old.sha, "--output", str(tmp_path)
    )
    assert failure.returncode == 1 and failure.stderr and not failure.stdout


def test_cli_reports_missing_snapshot_and_invalid_arguments(
    cli_env: dict[str, str],
) -> None:
    for args, status in [
        (("export", "unknown/repo", "--sha", "a" * 40), 3),
        (("export", REPO, "--sha", "a" * 40, "--format", "pdf"), 2),
        (("analyze", "https://github.com/" + REPO, "--sha", "short"), 2),
    ]:
        reply = run_cli(cli_env, *args)
        assert reply.returncode == status and reply.stderr and not reply.stdout
