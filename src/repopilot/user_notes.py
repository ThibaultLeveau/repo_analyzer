import hashlib
import re
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from repopilot.official_docs import MAX_CHUNKS, Excerpt, excerpts

MAX_BYTES = 128 * 1024
MEDIA = {"text/markdown", "text/x-markdown", "text/plain", "application/octet-stream"}


class NoteError(ValueError):
    pass


class RevisionConflict(NoteError):
    pass


@dataclass(frozen=True)
class Note:
    id: str
    filename: str
    revision: str
    uploaded: float

    def url(self, repo: str, sha: str) -> str:
        return f"/repos/{repo}/{sha}/getting-started?" + urlencode(
            {"note": self.id, "revision": self.revision}
        )


def _sections(filename: str, content: str) -> list[Excerpt]:
    html = MarkdownIt("commonmark", {"html": False}).render(content)
    document = BeautifulSoup(html, "html.parser")
    for number, heading in enumerate(document.find_all(re.compile("^h[1-6]$")), 1):
        heading["id"] = f"heading-{number}"
    for image in document.find_all("img"):
        image.replace_with(str(image.get("alt", "")))
    parts = excerpts("", str(document), "text/html")
    if not parts:
        raise NoteError("Include at least one paragraph of Markdown text.")
    if len(parts) == MAX_CHUNKS:
        raise NoteError("The note contains too many sections for the index.")
    return [Excerpt(filename, p.section or filename, p.url, p.text) for p in parts]


class Notes:
    def __init__(self, directory: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = directory / "user-notes.db"
        self.clock = clock
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY,repo TEXT NOT NULL,current TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS notes_repo ON notes(repo);
                CREATE TABLE IF NOT EXISTS revisions(
                    note TEXT NOT NULL, digest TEXT NOT NULL, filename TEXT NOT NULL, uploaded REAL NOT NULL,
                    original BLOB NOT NULL,
                    PRIMARY KEY(note,digest));
                CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
                    note UNINDEXED, digest UNINDEXED, section, anchor UNINDEXED, text);
            """)

    def save(
        self,
        repo: str,
        filename: str,
        media: str,
        data: bytes,
        *,
        note_id: str = "",
        expected: str = "",
    ) -> Note:
        if (
            not filename.lower().endswith((".md", ".markdown"))
            or len(filename) > 180
            or re.search(r"[/\\\x00-\x1f]", filename)
            or media not in MEDIA
        ):
            raise NoteError("Select a Markdown file (.md or .markdown).")
        if len(data) > MAX_BYTES:
            raise NoteError("The Markdown file exceeds 128 KiB.")
        try:
            content = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise NoteError("Markdown files must use UTF-8.") from exc
        if any(ord(c) < 32 and c not in "\t\r\n" for c in content):
            raise NoteError(
                "The Markdown file contains unsupported control characters."
            )
        parts = _sections(filename, content)
        digest = hashlib.sha256(data).hexdigest()
        note = Note(note_id or uuid.uuid4().hex, filename, digest, self.clock())
        with closing(sqlite3.connect(self.path)) as db, db:
            if note_id:
                if not db.execute(
                    "UPDATE notes SET current=? WHERE id=? AND repo=? AND current=?",
                    (digest, note_id, repo.lower(), expected),
                ).rowcount:
                    raise RevisionConflict(
                        "The note changed or was removed. Reload it before replacing it."
                    )
            else:
                db.execute(
                    "INSERT INTO notes VALUES(?,?,?)", (note.id, repo.lower(), digest)
                )
            added = db.execute(
                "INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?)",
                (note.id, digest, note.filename, note.uploaded, data),
            ).rowcount
            if added:
                db.executemany(
                    "INSERT INTO passages VALUES(?,?,?,?,?)",
                    ((note.id, digest, p.section, p.url, p.text) for p in parts),
                )
            else:
                name, when = db.execute(
                    "SELECT filename,uploaded FROM revisions WHERE note=? AND digest=?",
                    (note.id, digest),
                ).fetchone()
                return Note(note.id, name, digest, when)
        return note

    def original(self, repo: str, note_id: str, revision: str) -> bytes:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT original FROM revisions JOIN notes ON id=note WHERE repo=? AND id=? AND digest=?",
                (repo.lower(), note_id, revision),
            ).fetchone()
            if not row:
                raise KeyError("User note revision unavailable")
            return bytes(row[0])

    def remove(self, repo: str, note_id: str, expected: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            if not db.execute(
                "DELETE FROM notes WHERE repo=? AND id=? AND current=?",
                (repo.lower(), note_id, expected),
            ).rowcount:
                raise RevisionConflict(
                    "The note changed or was removed. Reload before removing it."
                )
            db.execute("DELETE FROM passages WHERE note=?", (note_id,))
            db.execute("DELETE FROM revisions WHERE note=?", (note_id,))

    def for_project(self, repo: str) -> list[Note]:
        with closing(sqlite3.connect(self.path)) as db:
            return [
                Note(*row)
                for row in db.execute(
                    """SELECT id,filename,digest,uploaded FROM notes JOIN revisions
                   ON note=id AND digest=current WHERE repo=? ORDER BY filename,id""",
                    (repo.lower(),),
                )
            ]

    def read(
        self, repo: str, note_id: str, revision: str = ""
    ) -> tuple[Note, list[Excerpt]]:
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("BEGIN")
            row = db.execute(
                """SELECT id,filename,digest,uploaded FROM notes JOIN revisions
                                ON note=id AND digest=coalesce(nullif(?,''),current) WHERE repo=? AND id=?""",
                (revision, repo.lower(), note_id),
            ).fetchone()
            if not row:
                raise KeyError("User note revision unavailable")
            note = Note(*row)
            sections = [
                Excerpt(note.filename, *part)
                for part in db.execute(
                    "SELECT section,anchor,text FROM passages WHERE note=? AND digest=? ORDER BY rowid",
                    (note.id, note.revision),
                )
            ]
        return note, sections

    def search(
        self, repo: str, question: str, limit: int = 6
    ) -> list[tuple[Note, Excerpt]]:
        terms = list(dict.fromkeys(re.findall(r"\w+", question[:500])))[:24]
        if not terms:
            return []
        match = " OR ".join(f'"{term}"' for term in terms)
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                """
                SELECT n.id,r.filename,r.digest,r.uploaded,p.section,p.anchor,p.text FROM passages p
                JOIN notes n ON n.id=p.note AND n.current=p.digest
                JOIN revisions r ON r.note=n.id AND r.digest=n.current
                WHERE n.repo=? AND passages MATCH ? ORDER BY rank,p.rowid LIMIT ?
            """,
                (repo.lower(), match, max(0, min(limit, MAX_CHUNKS))),
            )
            return [(Note(*row[:4]), Excerpt(row[1], *row[4:])) for row in rows]
