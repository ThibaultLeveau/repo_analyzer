import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from repopilot.models import RepositoryHead

JobStatus = Literal["queued", "running", "completed", "failed"]

_SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE IF NOT EXISTS repositories (
    name TEXT PRIMARY KEY COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS snapshots (
    repository TEXT NOT NULL REFERENCES repositories(name) COLLATE NOCASE,
    sha TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (repository, sha)
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    repository TEXT NOT NULL REFERENCES repositories(name) COLLATE NOCASE,
    ref TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sha TEXT,
    error TEXT,
    FOREIGN KEY (repository, sha) REFERENCES snapshots(repository, sha)
);
CREATE UNIQUE INDEX IF NOT EXISTS active_job ON jobs(repository, ref)
    WHERE status IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS import_sources (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    full_name TEXT NOT NULL,
    default_branch TEXT NOT NULL,
    sha TEXT NOT NULL,
    fetched INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS module_roles (
    repository TEXT COLLATE NOCASE, sha TEXT, model TEXT, path TEXT,
    description TEXT NOT NULL, ranges TEXT NOT NULL,
    PRIMARY KEY (repository, sha, model, path)
);
CREATE TABLE IF NOT EXISTS guide_notes (
    repository TEXT COLLATE NOCASE, sha TEXT, model TEXT, notes TEXT NOT NULL,
    PRIMARY KEY (repository, sha, model)
);
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY, repository TEXT COLLATE NOCASE NOT NULL, sha TEXT NOT NULL,
    question TEXT NOT NULL, answer TEXT NOT NULL, citations TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_snapshots (
    repository TEXT PRIMARY KEY COLLATE NOCASE, sha TEXT NOT NULL, job_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refreshes (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), previous_repo TEXT NOT NULL, previous_sha TEXT NOT NULL, manifest TEXT
);
CREATE TABLE IF NOT EXISTS vector_requests (
    repository TEXT COLLATE NOCASE, sha TEXT, PRIMARY KEY(repository, sha)
);
CREATE TRIGGER IF NOT EXISTS job_transition_time AFTER UPDATE OF status ON jobs
BEGIN
    UPDATE jobs SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = NEW.id;
END;
COMMIT;
"""


@dataclass(frozen=True)
class Job:
    id: int
    repository: str
    ref: str
    status: JobStatus
    created_at: str
    updated_at: str
    sha: str | None
    error: str | None


@dataclass(frozen=True)
class Snapshot:
    repository: str
    sha: str
    created_at: str


@dataclass(frozen=True)
class RefreshInfo:
    previous_repo: str
    previous_sha: str
    manifest: str | None


class JobStateError(ValueError):
    """The job no longer has the state needed for this transition."""


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db

    def submit(
        self,
        repository: str,
        ref: str = "HEAD",
        *,
        pinned: RepositoryHead | None = None,
        previous_sha: str | None = None,
    ) -> tuple[Job, bool]:
        """Return the active job and whether this caller created it."""
        previous_repo = repository.strip()
        repository = previous_repo.lower()
        if previous_sha is not None:
            ref = "refresh"
        elif pinned is not None:
            ref = pinned.sha
        if not repository or not ref:
            raise ValueError("A repository and reference are required")
        with self._connection(write=True) as db:
            active = db.execute(
                "SELECT * FROM jobs WHERE repository = ? AND ref = ? "
                "AND status IN ('queued', 'running')",
                (repository, ref),
            ).fetchone()
            if active:
                return Job(**active), False
            db.execute("INSERT OR IGNORE INTO repositories VALUES (?)", (repository,))
            row = db.execute(
                "INSERT INTO jobs(repository, ref, status) VALUES (?, ?, 'queued') RETURNING *",
                (repository, ref),
            ).fetchone()
            job = Job(**row)
            if previous_sha is not None:
                db.execute(
                    "INSERT INTO refreshes(job_id,previous_repo,previous_sha) VALUES (?,?,?)",
                    (job.id, previous_repo, previous_sha),
                )
            if pinned is not None:
                self._pin(db, job.id, pinned)
            return job, True

    @staticmethod
    def _pin(db: sqlite3.Connection, job_id: int, head: RepositoryHead) -> None:
        db.execute(
            "INSERT INTO import_sources(job_id, full_name, default_branch, sha) VALUES (?,?,?,?)",
            (job_id, head.full_name, head.default_branch, head.sha),
        )

    def pin(self, job_id: int, head: RepositoryHead) -> None:
        with self._connection(write=True) as db:
            self._pin(db, job_id, head)

    def source(self, job_id: int) -> tuple[RepositoryHead | None, bool]:
        with self._connection() as db:
            row = db.execute(
                "SELECT full_name, default_branch, sha, fetched FROM import_sources WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                return None, False
            return RepositoryHead(row[0], row[1], row[2]), bool(row[3])

    def mark_fetched(self, job_id: int) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "UPDATE import_sources SET fetched = 1 WHERE job_id = ?", (job_id,)
            )

    def job(self, job_id: int) -> Job:
        with self._connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return Job(**row)

    def snapshots(self, repository: str) -> list[Snapshot]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM snapshots WHERE repository = ? ORDER BY created_at DESC, sha",
                (repository,),
            )
            return [Snapshot(**row) for row in rows]

    def module_roles(
        self, repository: str, sha: str, model: str
    ) -> dict[str, tuple[str, list[tuple[int, int]]]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT path, description, ranges FROM module_roles WHERE repository=? AND sha=? AND model=?",
                (repository, sha, model),
            )
            return {
                path: (description, json.loads(ranges))
                for path, description, ranges in rows
            }

    def save_module_roles(
        self,
        repository: str,
        sha: str,
        model: str,
        roles: dict[str, tuple[str, list[tuple[int, int]]]],
    ) -> None:
        with self._connection(write=True) as db:
            db.executemany(
                "INSERT OR REPLACE INTO module_roles VALUES (?,?,?,?,?,?)",
                (
                    (repository, sha, model, path, description, json.dumps(ranges))
                    for path, (description, ranges) in roles.items()
                ),
            )

    def guide_notes(
        self, repository: str, sha: str, model: str
    ) -> list[tuple[str, list[str]]]:
        with self._connection() as db:
            row = db.execute(
                "SELECT notes FROM guide_notes WHERE repository=? AND sha=? AND model=?",
                (repository, sha, model),
            ).fetchone()
        return [(text, keys) for text, keys in json.loads(row[0])] if row else []

    def save_guide_notes(
        self, repository: str, sha: str, model: str, notes: list[tuple[str, list[str]]]
    ) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "INSERT OR REPLACE INTO guide_notes VALUES (?,?,?,?)",
                (repository, sha, model, json.dumps(notes)),
            )

    def save_question(
        self,
        repository: str,
        sha: str,
        question: str,
        answer: str,
        citations: list[str],
    ) -> int:
        with self._connection(write=True) as db:
            row = db.execute(
                "INSERT INTO questions(repository,sha,question,answer,citations) VALUES (?,?,?,?,?) RETURNING id",
                (repository, sha, question, answer, json.dumps(citations)),
            ).fetchone()
            return int(row[0])

    def question(self, repository: str, sha: str, answer_id: int) -> tuple[str, str]:
        with self._connection() as db:
            row = db.execute(
                "SELECT question,answer FROM questions WHERE repository=? AND sha=? AND id=?",
                (repository, sha, answer_id),
            ).fetchone()
        if row is None:
            raise KeyError(answer_id)
        return row[0], row[1]

    def recent_imports(self) -> list[Job]:
        with self._connection() as db:
            return [
                Job(**row)
                for row in db.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 20")
            ]

    def start(self, job_id: int) -> None:
        with self._connection(write=True) as db:
            changed = db.execute(
                "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
                (job_id,),
            ).rowcount
            if not changed:
                raise JobStateError("Only queued jobs can start")

    def active_sha(self, repository: str) -> str | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT sha FROM active_snapshots WHERE repository=?", (repository,)
            ).fetchone()
            if row is None:
                row = db.execute(
                    "SELECT sha FROM jobs WHERE repository=? AND status='completed' ORDER BY id DESC LIMIT 1",
                    (repository,),
                ).fetchone()
        return str(row[0]) if row else None

    def refresh_info(self, job_id: int) -> RefreshInfo | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT previous_repo,previous_sha,manifest FROM refreshes WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return RefreshInfo(**row) if row else None

    def remember_vectors(self, repository: str, sha: str) -> None:
        with self._connection(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO vector_requests VALUES (?,?)", (repository, sha)
            )

    def refresh_features(
        self, repository: str, sha: str
    ) -> tuple[set[str], bool, bool]:
        with self._connection() as db:
            paths = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT path FROM module_roles WHERE repository=? AND sha=?",
                    (repository, sha),
                )
            }
            guide = db.execute(
                "SELECT 1 FROM guide_notes WHERE repository=? AND sha=? LIMIT 1",
                (repository, sha),
            ).fetchone()
            vectors = db.execute(
                "SELECT 1 FROM vector_requests WHERE repository=? LIMIT 1",
                (repository,),
            ).fetchone()
        return paths, bool(guide), bool(vectors)

    def complete(self, job_id: int, sha: str, *, manifest: str | None = None) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("A full lowercase commit SHA is required")
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT COALESCE(s.full_name, j.repository), s.sha FROM jobs j "
                "LEFT JOIN import_sources s ON j.id = s.job_id WHERE j.id = ? AND j.status = 'running'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobStateError("Only running jobs can complete")
            repository, pinned_sha = row
            if pinned_sha is not None and pinned_sha != sha:
                raise JobStateError("Commit differs from the pinned source")
            db.execute("INSERT OR IGNORE INTO repositories VALUES (?)", (repository,))
            db.execute(
                "INSERT OR IGNORE INTO snapshots(repository, sha) VALUES (?,?)",
                (repository, sha),
            )
            db.execute(
                "UPDATE jobs SET status = 'completed', repository = ?, sha = ? WHERE id = ?",
                (repository, sha, job_id),
            )
            db.execute(
                "INSERT INTO active_snapshots VALUES (?,?,?) ON CONFLICT(repository) DO UPDATE SET sha=excluded.sha,job_id=excluded.job_id WHERE excluded.job_id>=active_snapshots.job_id",
                (repository, sha, job_id),
            )
            if manifest is not None:
                db.execute(
                    "UPDATE refreshes SET manifest=? WHERE job_id=?", (manifest, job_id)
                )

    def fail(self, job_id: int, message: str) -> None:
        message = " ".join(message.split())[:240] or "Analysis failed"
        with self._connection(write=True) as db:
            changed = db.execute(
                "UPDATE jobs SET status = 'failed', error = ? "
                "WHERE id = ? AND status IN ('queued', 'running')",
                (message, job_id),
            ).rowcount
            if not changed:
                raise JobStateError("Only active jobs can fail")

    def recover_interrupted(self) -> list[int]:
        """At worker restart, fail interrupted runs and return queued jobs to dispatch."""
        with self._connection(write=True) as db:
            db.execute(
                "UPDATE jobs SET status = 'failed', error = 'Interrupted during analysis' "
                "WHERE status = 'running'"
            )
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM jobs WHERE status = 'queued' ORDER BY id"
                )
            ]
