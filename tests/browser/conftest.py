import socket
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Thread

import httpx2
import pytest
import uvicorn

from repopilot.app import create_app
from repopilot.grounding import StructuredModel


@pytest.fixture
def import_transport() -> httpx2.BaseTransport | None:
    return None


@pytest.fixture
def chat_model() -> StructuredModel | None:
    return None


@pytest.fixture
def app_clock() -> Callable[[], float]:
    return time.time


@pytest.fixture
def app_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    import_transport: httpx2.BaseTransport | None,
    chat_model: StructuredModel | None,
    app_clock: Callable[[], float],
) -> Iterator[str]:
    monkeypatch.setenv("REPOPILOT_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(
                    transport=import_transport, model=chat_model, clock=app_clock
                ),
                log_level="error",
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while (
                thread.is_alive() and not server.started and time.monotonic() < deadline
            ):
                time.sleep(0.02)
            assert server.started, "Test server did not start"
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "Test server did not stop"
