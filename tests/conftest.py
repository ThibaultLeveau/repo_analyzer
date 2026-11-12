import io
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
            writer.writestr("demo/bin.dat", b"\0")
            writer.writestr("demo/large.log", "x" * 4097)
        with httpx2.Client(transport=archive_transport(archive.getvalue())) as client:
            cache.fetch(RepositoryHead("Owner/Demo", "main", sha), client)
    return "/repos/Owner/Demo/" + "a" * 40 + "/code"
