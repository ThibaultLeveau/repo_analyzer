import hashlib
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlsplit

import httpx2
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field, TypeAdapter

from repopilot.github import GitHubError, RateLimited, api_get, repository_name

WINDOW = 50
Action = Literal["load", "refresh", "extend"]


class Issue(BaseModel):
    id: int = Field(gt=0)
    number: int = Field(gt=0)
    title: str
    body: str
    labels: list[str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IssueQueue:
    repository: str
    issues: tuple[Issue, ...]
    fetched_at: float | None
    interval_hours: float
    retry_at: float
    exhausted: bool

    @property
    def next_at(self) -> float:
        cooldown = (
            self.fetched_at + self.interval_hours * 3600
            if self.fetched_at is not None
            else 0
        )
        return max(cooldown, self.retry_at)


class QueueBusy(GitHubError):
    pass


class Cooldown(GitHubError):
    def __init__(self, until: float) -> None:
        self.until = until
        super().__init__("The issue queue is cooling down.")


def _parse(response: httpx2.Response) -> list[Issue]:
    try:
        rows = response.json()
        if not isinstance(rows, list):
            raise TypeError
        issues = []
        for row in rows:
            if "pull_request" in row or row["state"] != "open":
                continue
            issues.append(
                Issue.model_validate(
                    {
                        **row,
                        "body": row.get("body") or "",
                        "labels": [label["name"] for label in row["labels"]],
                    },
                    strict=True,
                )
            )
        return issues
    except (TypeError, KeyError, ValueError) as exc:
        raise GitHubError("Invalid GitHub issue page") from exc


def _next(response: httpx2.Response, repo: str, current: int) -> int | None:
    if not (link := response.links.get("next")):
        return None
    try:
        url = urlsplit(link["url"])
        query = parse_qs(url.query)
        trusted_path = url.path.lower() == f"/repos/{repo}/issues" or re.fullmatch(
            r"/repositories/[0-9]+/issues", url.path
        )
        if (
            url.scheme != "https"
            or url.netloc.lower() != "api.github.com"
            or not trusted_path
            or url.fragment
            or query.get("page") != [str(current + 1)]
        ):
            raise ValueError
    except (KeyError, ValueError) as exc:
        raise GitHubError("Unexpected GitHub issue pagination link") from exc
    # Rebuild requests against the original repository with the original ordering.
    return current + 1


_SCHEMA = """
CREATE TABLE IF NOT EXISTS queues (
    repository TEXT PRIMARY KEY, fetched_at REAL, interval_hours REAL NOT NULL DEFAULT 6,
    retry_at REAL NOT NULL DEFAULT 0, next_page INTEGER DEFAULT 1, pending TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS issues (
    repository TEXT NOT NULL, id INTEGER NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(repository,id)
);
"""
_PENDING = TypeAdapter(list[Issue])


class IssuePages:
    def __init__(
        self,
        directory: Path,
        client: httpx2.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = directory / "issues.db"
        self.client = client
        self.clock = clock
        directory.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def _repo(self, name: str) -> str:
        return repository_name(f"https://github.com/{name}").lower()

    def read(self, name: str) -> IssueQueue:
        repo = self._repo(name)
        with self._db() as db:
            db.execute("BEGIN")
            row = db.execute(
                "SELECT * FROM queues WHERE repository=?", (repo,)
            ).fetchone()
            issues = tuple(
                Issue.model_validate_json(r[0])
                for r in db.execute(
                    "SELECT payload FROM issues WHERE repository=? ORDER BY created_at DESC,id DESC",
                    (repo,),
                )
            )
        if row is None:
            return IssueQueue(repo, (), None, 6, 0, False)
        return IssueQueue(
            repo,
            issues,
            row["fetched_at"],
            row["interval_hours"],
            row["retry_at"],
            row["next_page"] is None and row["pending"] == "[]",
        )

    def set_interval(self, name: str, hours: float) -> None:
        if not math.isfinite(hours) or hours <= 0 or not math.isfinite(hours * 3600):
            raise ValueError("Enter a positive, finite number of hours.")
        with self._db() as db:
            db.execute(
                "INSERT INTO queues(repository,interval_hours) VALUES (?,?) "
                "ON CONFLICT(repository) DO UPDATE SET interval_hours=excluded.interval_hours",
                (self._repo(name), hours),
            )

    def load(self, name: str, action: Action = "load") -> IssueQueue:
        repo = self._repo(name)
        lock = self.path.parent / (
            "issues-" + hashlib.sha256(repo.encode()).hexdigest() + ".lock"
        )
        try:
            with FileLock(lock, timeout=0):
                return self._load(repo, action)
        except Timeout as exc:
            raise QueueBusy(
                "An issue request is already running for this repository."
            ) from exc

    def _load(self, repo: str, action: Action) -> IssueQueue:
        previous = self.read(repo)
        if action == "load" and previous.fetched_at is not None:
            return previous
        if action == "extend" and previous.exhausted:
            return previous
        if self.clock() < previous.next_at:
            raise Cooldown(previous.next_at)
        page: int | None = 1
        pending: list[Issue] = []
        seen: set[int] = set()
        if action == "extend" and previous.fetched_at is not None:
            seen.update(i.id for i in previous.issues)
            with self._db() as db:
                row = db.execute(
                    "SELECT next_page,pending FROM queues WHERE repository=?", (repo,)
                ).fetchone()
                page, pending = row[0], _PENDING.validate_json(row[1])
        incoming: list[Issue] = []
        try:
            while len(incoming) < WINDOW:
                if not pending:
                    if page is None:
                        break
                    url = f"https://api.github.com/repos/{repo}/issues?state=open&sort=created&direction=desc&per_page=100&page={page}"
                    response = api_get(self.client, url)
                    pending = _parse(response)
                    page = _next(response, repo, page)
                fresh = []
                for issue in pending:
                    if issue.id not in seen:
                        seen.add(issue.id)
                        fresh.append(issue)
                space = WINDOW - len(incoming)
                incoming.extend(fresh[:space])
                pending = fresh[space:]
        except RateLimited as exc:
            now = self.clock()
            until = max(
                now + int(exc.retry_after or 0)
                if (exc.retry_after or "").isdigit()
                else 0,
                int(exc.reset_at or 0) if (exc.reset_at or "").isdigit() else 0,
            )
            with self._db() as db:
                db.execute(
                    "INSERT INTO queues(repository,retry_at) VALUES (?,?) "
                    "ON CONFLICT(repository) DO UPDATE SET retry_at=excluded.retry_at",
                    (repo, until if until > now else now + 60),
                )
            raise
        with self._db() as db:
            if action != "extend":
                db.execute("DELETE FROM issues WHERE repository=?", (repo,))
            db.executemany(
                "INSERT INTO issues VALUES (?,?,?,?)",
                ((repo, i.id, i.created_at, i.model_dump_json()) for i in incoming),
            )
            db.execute(
                "INSERT INTO queues(repository,fetched_at,next_page,pending) VALUES (?,?,?,?) "
                "ON CONFLICT(repository) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "next_page=excluded.next_page,pending=excluded.pending,retry_at=0",
                (repo, self.clock(), page, _PENDING.dump_json(pending).decode()),
            )
        return self.read(repo)
