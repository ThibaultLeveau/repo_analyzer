import hashlib
import re
import sqlite3
import stat
import unicodedata
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

import httpx2

from repopilot.github import RepositoryHead, repository_name

EntryKind = Literal["text", "directory", "binary", "oversized", "special"]
_DEVICE = re.compile(
    r"(?:CON|AUX|PRN|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])", re.IGNORECASE
)


class SnapshotError(ValueError):
    """The source archive cannot be used as a snapshot."""


@dataclass(frozen=True)
class Limits:
    download_bytes: int = 32 << 20
    expanded_bytes: int = 128 << 20
    entries: int = 20_000
    file_bytes: int = 1 << 20


@dataclass(frozen=True)
class Entry:
    path: str
    kind: EntryKind
    size: int


@dataclass(frozen=True)
class SourceSnapshot:
    database: Path

    def _open(self) -> sqlite3.Connection:
        uri = self.database.resolve().as_uri() + "?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True)

    def tree(self) -> list[Entry]:
        with closing(self._open()) as db:
            return [
                Entry(*row)
                for row in db.execute(
                    "SELECT path, kind, size FROM entries ORDER BY path"
                )
            ]

    def read_text(self, path: str) -> str:
        with closing(self._open()) as db:
            row = db.execute(
                "SELECT kind, content FROM entries WHERE path = ?", (path,)
            ).fetchone()
        if row is None:
            raise FileNotFoundError(path)
        kind, content = row
        if not isinstance(content, str):
            raise SnapshotError(f"Cannot display {kind} entry")
        return content


def _parts(name: str) -> list[str]:
    parts = name.removesuffix("/").split("/")
    if len(parts) > 128 or len(name) > 4096:
        raise SnapshotError("Archive path exceeds the limit")
    for part in parts:
        if (
            not part
            or part in {".", ".."}
            or part.endswith((" ", "."))
            or re.search(r'[\\:*?"<>|\x00-\x1f]', part)
            or _DEVICE.fullmatch(part.partition(".")[0].rstrip(" "))
        ):
            raise SnapshotError("Archive contains an unsafe path")
    return parts


def _content(
    archive: zipfile.ZipFile, item: zipfile.ZipInfo, limits: Limits
) -> tuple[EntryKind, str | None]:
    if item.flag_bits & 1:
        raise SnapshotError("Encrypted source archive is unsupported")
    mode = stat.S_IFMT(item.external_attr >> 16)
    if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
        return "special", None
    if item.is_dir():
        return "directory", None
    if item.file_size > limits.file_bytes:
        return "oversized", None
    with archive.open(item) as member:
        data = member.read(limits.file_bytes + 1)
    if len(data) != item.file_size:
        raise SnapshotError("Archive file size mismatch")
    if b"\0" not in data:
        try:
            return "text", data.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass
    return "binary", None


def _index(zip_path: Path, database: Path, limits: Limits) -> None:
    with (
        zipfile.ZipFile(zip_path) as archive,
        closing(sqlite3.connect(database)) as db,
        db,
    ):
        items = archive.infolist()
        if (
            len(items) > limits.entries
            or sum(i.file_size for i in items) > limits.expanded_bytes
        ):
            raise SnapshotError("Expanded archive exceeds the limit")
        db.execute(
            "CREATE TABLE entries(path TEXT PRIMARY KEY, kind TEXT, size INTEGER, content TEXT)"
        )
        root: str | None = None
        seen: dict[str, tuple[str, EntryKind]] = {}
        for item in items:
            prefix, *parts = _parts(item.orig_filename)
            if root is None:
                root = prefix
            if prefix != root or (not parts and not item.is_dir()):
                raise SnapshotError("Expected one repository root directory")
            kind, content = _content(archive, item, limits)
            for depth in range(1, len(parts) + 1):
                path = "/".join(parts[:depth])
                key = unicodedata.normalize("NFC", path).casefold()
                leaf = depth == len(parts)
                entry_kind: EntryKind = kind if leaf else "directory"
                previous = seen.get(key)
                if previous:
                    if previous != (path, "directory") or entry_kind != "directory":
                        raise SnapshotError("Archive contains conflicting paths")
                    continue
                if len(seen) >= limits.entries:
                    raise SnapshotError("Archive entry count exceeds the limit")
                seen[key] = (path, entry_kind)
                db.execute(
                    "INSERT INTO entries VALUES (?,?,?,?)",
                    (
                        path,
                        entry_kind,
                        item.file_size if leaf else 0,
                        content if leaf else None,
                    ),
                )
        if root is None:
            raise SnapshotError("Archive is missing its repository root")


class SnapshotCache:
    def __init__(self, root: Path, limits: Limits = Limits()) -> None:
        self.root = root
        self.limits = limits

    def _location(self, repository: str, sha: str) -> Path:
        name = repository_name(f"https://github.com/{repository}")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SnapshotError("Expected a full commit SHA")
        identity = hashlib.sha256(name.lower().encode()).hexdigest()
        return self.root / identity / sha

    def open(self, repository: str, sha: str) -> SourceSnapshot:
        database = self._location(repository, sha) / "source.db"
        if not database.is_file():
            raise FileNotFoundError("Snapshot has not been imported")
        return SourceSnapshot(database)

    def fetch(self, head: RepositoryHead, client: httpx2.Client) -> SourceSnapshot:
        target = self._location(head.full_name, head.sha)
        database = target / "source.db"
        if database.is_file():
            return SourceSnapshot(database)
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target.parent) as temporary:
            stage = Path(temporary)
            archive = stage / "source.zip"
            url = f"https://codeload.github.com/{head.full_name}/zip/{head.sha}"
            try:
                with client.stream(
                    "GET",
                    url,
                    headers={"Accept-Encoding": "identity"},
                    follow_redirects=False,
                    timeout=30,
                ) as response:
                    response.raise_for_status()
                    with archive.open("wb") as output:
                        for chunk in response.iter_raw(64 << 10):
                            if output.tell() + len(chunk) > self.limits.download_bytes:
                                raise SnapshotError(
                                    "Archive download exceeds the limit"
                                )
                            output.write(chunk)
                _index(archive, stage / "source.db", self.limits)
            except (httpx2.HTTPError, zipfile.BadZipFile, NotImplementedError) as exc:
                raise SnapshotError("Could not read the pinned source archive") from exc
            archive.unlink()
            try:
                stage.rename(target)
            except OSError:
                # Another importer may have published this same commit first.
                if not database.is_file():
                    raise
        return SourceSnapshot(database)
