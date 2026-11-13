"""Launch RepoPilot from a source checkout using only the Python standard library."""

import argparse
import errno
import hashlib
import os
import signal
import socket
import subprocess
import sys
import time
import venv
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def install_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.seek(0)
            while True:
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                    time.sleep(0.25)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def prepare(project: Path, data: Path) -> Path:
    checkout = hashlib.sha256(os.fsencode(project)).hexdigest()[:16]
    env = (
        data
        / "runtime"
        / f"{checkout}-py{sys.version_info.major}{sys.version_info.minor}"
    )
    env.mkdir(parents=True, exist_ok=True)
    python = env / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    signature = hashlib.sha256((project / "pyproject.toml").read_bytes()).hexdigest()
    marker = env / "installed.txt"
    with install_lock(env / "install.lock"):
        if python.exists() and marker.is_file() and marker.read_text() == signature:
            return python
        marker.unlink(missing_ok=True)
        venv.EnvBuilder(with_pip=True).create(env)
        subprocess.run(
            [str(python), "-m", "pip", "install", "-e", str(project)], check=True
        )
        marker.write_text(signature)
    return python


def serve(data: Path, port: int) -> int:
    import uvicorn

    from repopilot.app import create_app
    from repopilot.vectors import initialize_chroma

    with socket.socket() as listener:
        if sys.platform == "win32":
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            print(
                f"Cannot listen on 127.0.0.1:{port} ({exc}). Choose another --port.",
                file=sys.stderr,
            )
            return 1
        initialize_chroma(data)
        server = uvicorn.Server(
            uvicorn.Config(create_app(data_dir=data), log_level="info")
        )
        print(f"Open http://127.0.0.1:{port}/", flush=True)
        # Uvicorn replays the shutdown signal after draining requests.
        if sys.platform == "win32":
            signal.signal(signal.SIGBREAK, lambda *_: None)
        else:
            signal.signal(signal.SIGTERM, lambda *_: None)
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            pass
        return 0 if server.started else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("REPOPILOT_DATA_DIR", Path.home() / ".repopilot")),
    )
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info < (3, 12):  # noqa: UP036 -- the launcher runs before installation.
        parser.error("RepoPilot requires Python 3.12 or later.")
    if not 1 <= args.port <= 65535:
        parser.error("Choose a port between 1 and 65535.")
    directory = args.data_dir.expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if args.serve:
        return serve(directory, args.port)
    project = Path(__file__).resolve().parent
    python = prepare(project, directory)
    argv = [
        str(python),
        str(project / "run.py"),
        "--serve",
        "--port",
        str(args.port),
        "--data-dir",
        str(directory),
    ]
    if os.name != "nt":
        os.execv(python, argv)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, lambda *_: None)
    return subprocess.run(argv, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except subprocess.CalledProcessError as exc:
        sys.exit(
            f"Installation failed (exit {exc.returncode}). Run the same command to retry."
        )
