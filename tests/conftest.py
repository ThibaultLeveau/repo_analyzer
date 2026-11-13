import io
import os
import zipfile
from pathlib import Path

import httpx2
import pytest

from repopilot.models import RepositoryHead
from repopilot.snapshots import Limits, SnapshotCache


def archive_transport(content: bytes) -> httpx2.MockTransport:
    return httpx2.MockTransport(lambda _: httpx2.Response(200, stream=httpx2.ByteStream(content)))


@pytest.fixture
def explorer_url(tmp_path: Path) -> str:
    cache = SnapshotCache(tmp_path / "sources", Limits(file_bytes=4096))
    for sha, marker in (("a" * 40, "original"), ("b" * 40, "updated")):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as writer:
            writer.writestr("demo/README.md", "# How to run")
            writer.writestr(
                "demo/src/demo.py", f'# {marker}\r\nmsg = "<tag>"\n' + "\n" * 199
            )
            writer.writestr("demo/A & B.txt", "")
            writer.writestr("demo/tests/check.py", "import yaml\n")
            writer.writestr("demo/requirements.txt", "PyYAML>=6\n")
            writer.writestr("demo/bin.dat", b"\0")
            writer.writestr("demo/large.log", "x" * 4097)
        with httpx2.Client(transport=archive_transport(archive.getvalue())) as client:
            cache.fetch(RepositoryHead("Owner/Demo", "main", sha), client)
    return "/repos/Owner/Demo/" + "a" * 40 + "/code"


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
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(Path(__file__).resolve().parent)))
    env["REPOPILOT_DATA_DIR"] = str(tmp_path / "analysis cache")
    return env
