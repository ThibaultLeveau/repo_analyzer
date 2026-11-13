import importlib.util
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import venv
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from shutil import copyfile, copytree

import httpx2
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_retry_after_interrupted_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "run.py")
    assert spec and spec.loader
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    counter = 0

    def install(argv: list[str], check: bool) -> None:
        nonlocal counter
        assert check and argv[1:] == ["-m", "pip", "install", "-e", str(ROOT)]
        counter += 1
        if counter == 1:
            raise subprocess.CalledProcessError(1, argv)

    builder = venv.EnvBuilder(with_pip=False)
    monkeypatch.setattr(launcher.subprocess, "run", install)
    monkeypatch.setattr(launcher.venv, "EnvBuilder", lambda **_: builder)
    with pytest.raises(subprocess.CalledProcessError):
        launcher.prepare(ROOT, tmp_path)
    assert not list(tmp_path.rglob("installed.txt"))
    assert launcher.prepare(ROOT, tmp_path).is_file()
    assert launcher.prepare(ROOT, tmp_path).is_file() and counter == 2


@contextmanager
def running(
    command: list[str], cwd: Path, env: dict[str, str], port: int
) -> Iterator[None]:
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    with (
        (cwd / "launch.log").open("ab") as log,
        subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        ) as process,
    ):
        try:
            with httpx2.Client(timeout=1, trust_env=False) as client:
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        if client.get(f"http://127.0.0.1:{port}/health").json() == {
                            "status": "ok"
                        }:
                            break
                    except (httpx2.HTTPError, ValueError):
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail(
                        (cwd / "launch.log").read_text(errors="replace")[-3000:]
                    )
            yield
        finally:
            if process.poll() is None:
                if sys.platform == "win32":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    raise
        expected = (0, 0xC000013A) if sys.platform == "win32" else (0,)
        assert process.returncode in expected
        assert "Application shutdown complete." in (cwd / "launch.log").read_text(
            errors="replace"
        )


def test_one_command_cold_warm_persistence_and_port_error(tmp_path: Path) -> None:
    project = tmp_path / "source checkout"
    data = tmp_path / "state and cache"
    project.mkdir()
    copytree(ROOT / "src" / "repopilot", project / "src" / "repopilot")
    for name in ("run.py", "pyproject.toml", "README.md"):
        copyfile(ROOT / name, project / name)

    # Borrow the test runner's installed wheels so the subprocess smoke test can run offline.
    support = tmp_path / "prepared dependencies"
    support.mkdir()
    paths = [p for p in sys.path if Path(p).name in {"site-packages", "dist-packages"}]
    (support / "sitecustomize.py").write_text(
        f"import sys\nsys.path.extend({paths!r})\n", encoding="utf-8"
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(support),
        "PIP_NO_INDEX": "1",
        "PIP_NO_BUILD_ISOLATION": "0",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    env.pop("OPENAI_API_KEY", None)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    command = [
        sys.executable,
        str(project / "run.py"),
        "--port",
        str(port),
        "--data-dir",
        str(data),
    ]
    with running(command, tmp_path, env, port):
        assert (data / "state.db").is_file() and (
            data / "chroma" / "chroma.sqlite3"
        ).is_file()
        with sqlite3.connect(data / "issues.db") as db:
            db.execute(
                "INSERT INTO queues(repository,interval_hours) VALUES ('owner/repo',2)"
            )
        (marker,) = (data / "runtime").rglob("installed.txt")
        first_time = marker.stat().st_mtime_ns
        occupied = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, timeout=50, check=False
        )
        assert occupied.returncode != 0 and b"Choose another --port" in occupied.stderr
    with running(command, tmp_path, env, port):
        assert marker.stat().st_mtime_ns == first_time
        with sqlite3.connect(data / "issues.db") as db:
            assert db.execute("SELECT interval_hours FROM queues").fetchone() == (2,)
