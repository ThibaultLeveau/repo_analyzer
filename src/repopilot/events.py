import json
import logging
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import httpx2
from openai import DefaultHttpxClient


@dataclass
class Measurement:
    success: bool = True
    cache: Literal["hit", "miss"] | None = None
    openai_calls: int = 0
    retries: int = 0
    tokens: int | None = None


@dataclass(frozen=True)
class AnalysisEvent:
    operation: str
    timestamp: float
    duration_ms: int
    repository: str | None
    sha: str | None
    job_id: int | None
    metrics: Measurement


@dataclass(frozen=True)
class Totals:
    operations: int
    failures: int
    duration_ms: int
    cache_hits: int
    cache_misses: int
    openai_calls: int
    provider_failures: int
    retries: int
    reported_tokens: int | None
    calls_without_usage: int


class EventStore:
    def __init__(
        self, directory: Path, clock: Callable[[], float] = time.time, limit: int = 5000
    ) -> None:
        if limit < 1:
            raise ValueError("Event history requires a positive limit")
        self.path = directory / "events.db"
        self.clock = clock
        self.limit = limit
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(id INTEGER PRIMARY KEY, repository TEXT, timestamp REAL, payload TEXT)"
            )

    def append(self, event: AnalysisEvent) -> None:
        try:
            with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                row = db.execute(
                    "INSERT INTO events(repository,timestamp,payload) VALUES (?,?,?) RETURNING id",
                    (event.repository, event.timestamp, json.dumps(asdict(event))),
                ).fetchone()
                db.execute("DELETE FROM events WHERE id <= ?", (row[0] - self.limit,))
        except sqlite3.Error:
            logging.getLogger(__name__).warning("Local analysis metrics unavailable")

    def recent(
        self, repository: str | None = None, since: float = 0
    ) -> list[AnalysisEvent]:
        rows: list[AnalysisEvent] = []
        with closing(sqlite3.connect(self.path)) as db:
            for (payload,) in db.execute(
                "SELECT payload FROM events WHERE (? IS NULL OR repository=?) "
                "AND timestamp>=? ORDER BY id DESC",
                (repository, repository.lower() if repository else None, since),
            ):
                fields = json.loads(payload)
                metrics = Measurement(**fields.pop("metrics"))
                rows.append(AnalysisEvent(**fields, metrics=metrics))
        return rows

    def totals(self, repository: str | None = None, since: float = 0) -> Totals:
        events = self.recent(repository, since)
        metrics = [e.metrics for e in events]
        known = [m.tokens for m in metrics if m.tokens is not None]
        return Totals(
            operations=len(events),
            failures=sum(not m.success for m in metrics),
            duration_ms=sum(e.duration_ms for e in events),
            cache_hits=sum(m.cache == "hit" for m in metrics),
            cache_misses=sum(m.cache == "miss" for m in metrics),
            openai_calls=sum(m.openai_calls for m in metrics),
            provider_failures=sum(
                e.operation.startswith("model.") and not e.metrics.success
                for e in events
            ),
            retries=sum(m.retries for m in metrics),
            reported_tokens=sum(known) if known else None,
            calls_without_usage=sum(
                m.openai_calls - int(m.tokens is not None)
                for m in metrics
                if m.openai_calls
            ),
        )

    @contextmanager
    def scope(
        self,
        repository: str | None = None,
        sha: str | None = None,
        job_id: int | None = None,
    ) -> Iterator[None]:
        token = _scope.set(
            (self, repository.lower() if repository else None, sha, job_id)
        )
        try:
            yield
        finally:
            _scope.reset(token)


_scope: ContextVar[tuple[EventStore, str | None, str | None, int | None] | None] = (
    ContextVar("analysis_event_scope", default=None)
)


@contextmanager
def measure(operation: str) -> Iterator[Measurement]:
    metrics = Measurement()
    context = _scope.get()
    if context is None:
        yield metrics
        return
    store, repo, sha, job = context
    start = store.clock()
    try:
        yield metrics
    except Exception:
        metrics.success = False
        raise
    finally:
        end = store.clock()
        store.append(
            AnalysisEvent(
                operation,
                end,
                max(0, round((end - start) * 1000)),
                repo,
                sha,
                job,
                metrics,
            )
        )


def provider_client(metrics: Measurement) -> httpx2.Client:
    def count(request: httpx2.Request) -> None:
        metrics.openai_calls += 1
        if request.headers.get("x-stainless-retry-count", "0") != "0":
            metrics.retries += 1

    return DefaultHttpxClient(event_hooks={"request": [count]})
