import re
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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

    def submit(self, repository: str, ref: str = "HEAD") -> tuple[Job, bool]:
        """Return the active job and whether this caller created it."""
        repository = repository.strip().lower()
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
            return Job(**row), True

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

    def start(self, job_id: int) -> None:
        with self._connection(write=True) as db:
            changed = db.execute(
                "UPDATE jobs SET status = 'running' WHERE id = ? AND status = 'queued'",
                (job_id,),
            ).rowcount
            if not changed:
                raise JobStateError("Only queued jobs can start")

    def complete(self, job_id: int, sha: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("A full lowercase commit SHA is required")
        with self._connection(write=True) as db:
            db.execute(
                "INSERT OR IGNORE INTO snapshots(repository, sha) "
                "SELECT repository, ? FROM jobs WHERE id = ? AND status = 'running'",
                (sha, job_id),
            )
            changed = db.execute(
                "UPDATE jobs SET status = 'completed', sha = ? "
                "WHERE id = ? AND status = 'running'",
                (sha, job_id),
            ).rowcount
            if not changed:
                raise JobStateError("Only running jobs can complete")

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
