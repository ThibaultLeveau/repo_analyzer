import io
import stat
import zipfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import httpx2
import pytest

from repopilot.github import RepositoryHead
from repopilot.snapshots import Limits, SnapshotCache, SnapshotError

HEAD = RepositoryHead("Owner/Repo", "main", "1" * 40)
NEXT = RepositoryHead("Owner/Repo", "main", "2" * 40)


def archive(*files: tuple[str | zipfile.ZipInfo, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as writer:
        writer.writestr("repo/", b"")
        for name, data in files:
            if isinstance(name, str):
                entry = zipfile.ZipInfo(name)
                # Preserve the test's archive spelling on Windows.
                entry.filename = name
                writer.writestr(entry, data)
            else:
                writer.writestr(name, data)
    return buffer.getvalue()


def reply(data: bytes) -> httpx2.Response:
    return httpx2.Response(200, stream=httpx2.ByteStream(data))


def test_source_tree_is_pinned_and_read_only(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("repo/link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    data = archive(
        ("repo/src/hello.py", "héllo\r\n".encode()),
        ("repo/binary", b"a\0b"),
        ("repo/long", b"x" * 50),
        (link, b"../../outside"),
    )
    urls: list[str] = []

    def get(request: httpx2.Request) -> httpx2.Response:
        urls.append(str(request.url))
        return reply(data if len(urls) == 1 else archive(("repo/new", b"revision two")))

    cache = SnapshotCache(tmp_path, Limits(file_bytes=32))
    with httpx2.Client(transport=httpx2.MockTransport(get)) as client:
        first = cache.fetch(HEAD, client)
        assert first == cache.fetch(HEAD, client) == cache.open("OWNER/Repo", HEAD.sha)
        assert [(e.path, e.kind) for e in first.tree()] == [
            ("binary", "binary"),
            ("link", "special"),
            ("long", "oversized"),
            ("src", "directory"),
            ("src/hello.py", "text"),
        ]
        assert first.read_text("src/hello.py") == "héllo\r\n"
        for path in ("link", "binary", "long", "src"):
            with pytest.raises(SnapshotError):
                first.read_text(path)
        with pytest.raises(FileNotFoundError):
            first.read_text("../outside")
        assert cache.fetch(NEXT, client).read_text("new") == "revision two"
        assert first.read_text("src/hello.py") == "héllo\r\n"
    assert urls == [
        f"https://codeload.github.com/Owner/Repo/zip/{h.sha}" for h in (HEAD, NEXT)
    ]


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "repo/../evil",
        "/evil",
        "repo/f\\x",
        "repo/C:evil",
        "repo/con.txt",
        "repo/end.",
        "repo/a /b",
        "other/root",
        "repo/d/../../out",
    ],
)
def test_unsafe_members_leave_no_snapshot(tmp_path: Path, name: str) -> None:
    data = archive((name, b"unused"))
    cache = SnapshotCache(tmp_path)
    with (
        httpx2.Client(transport=httpx2.MockTransport(lambda _: reply(data))) as client,
        pytest.raises(SnapshotError),
    ):
        cache.fetch(HEAD, client)
    with pytest.raises(FileNotFoundError):
        cache.open(HEAD.full_name, HEAD.sha)


@pytest.mark.parametrize(
    ("data", "limits"),
    [
        (archive(("repo/a", b"x")), Limits(download_bytes=8)),
        (archive(("repo/a", b"x" * 100)), Limits(expanded_bytes=32)),
        (archive(("repo/a", b""), ("repo/b", b"")), Limits(entries=2)),
        (archive(("repo/A/f", b""), ("repo/a/g", b"")), Limits()),
        (archive(("repo/file", b""), ("repo/file/sub", b"")), Limits()),
        (b"not a zip archive", Limits()),
    ],
)
def test_invalid_archive_does_not_replace_completed_revision(
    tmp_path: Path, data: bytes, limits: Limits
) -> None:
    cache = SnapshotCache(tmp_path)
    replies = iter([archive(("repo/keep", b"original")), data])
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda _: reply(next(replies)))
    ) as client:
        stable = cache.fetch(HEAD, client)
        with pytest.raises(SnapshotError):
            SnapshotCache(tmp_path, limits).fetch(NEXT, client)
        assert stable.read_text("keep") == "original"
    with pytest.raises(FileNotFoundError):
        cache.open(HEAD.full_name, NEXT.sha)


def test_interrupted_stream_can_be_retried(tmp_path: Path) -> None:
    class BrokenStream(httpx2.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"partial"
            raise httpx2.ReadError("interrupted")

    responses = iter(
        [
            httpx2.Response(200, stream=BrokenStream()),
            reply(archive(("repo/file", b"ok"))),
        ]
    )
    cache = SnapshotCache(tmp_path)
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda _: next(responses))
    ) as client:
        with pytest.raises(SnapshotError):
            cache.fetch(HEAD, client)
        assert cache.fetch(HEAD, client).read_text("file") == "ok"


def test_simultaneous_writers_publish_one_complete_snapshot(tmp_path: Path) -> None:
    ready = Barrier(2)
    data = archive(("repo/file", b"ok"))

    def get(request: httpx2.Request) -> httpx2.Response:
        ready.wait(timeout=5)
        return reply(data)

    cache = SnapshotCache(tmp_path)
    with (
        httpx2.Client(transport=httpx2.MockTransport(get)) as client,
        ThreadPoolExecutor(2) as pool,
    ):
        first, second = pool.map(lambda _: cache.fetch(HEAD, client), range(2))
    assert first == second
    assert first.read_text("file") == "ok"
    assert list(first.database.parent.parent.iterdir()) == [first.database.parent]
