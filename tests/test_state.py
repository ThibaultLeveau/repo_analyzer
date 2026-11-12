import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
from threading import Barrier

import pytest

from repopilot.state import Job, JobStateError, StateStore

SHA = "ab" * 20


def test_completed_jobs_survive_reopening(tmp_path: Path) -> None:
    path = tmp_path / "data" / "state.db"
    store = StateStore(path)
    job, created = store.submit("Owner/Repo")
    assert created and job.status == "queued"
    assert store.submit("owner/repo") == (job, False)
    store.start(job.id)
    assert store.job(job.id).status == "running"
    with pytest.raises(JobStateError):
        store.start(job.id)
    with pytest.raises(ValueError):
        store.complete(job.id, "main")
    store.complete(job.id, SHA)
    reopened = StateStore(path)
    completed = reopened.job(job.id)
    assert (completed.status, completed.sha, completed.error) == (
        "completed",
        SHA,
        None,
    )
    assert datetime.fromisoformat(completed.updated_at) >= datetime.fromisoformat(
        job.created_at
    )
    (snapshot,) = reopened.snapshots("OWNER/REPO")
    assert (snapshot.repository, snapshot.sha) == ("owner/repo", SHA)
    with pytest.raises(JobStateError):
        reopened.fail(job.id, "obsolete")
    later, created = reopened.submit("owner/repo")
    assert created and later.id != job.id
    reopened.start(later.id)
    reopened.complete(later.id, SHA)
    assert reopened.snapshots("owner/repo") == [snapshot]


def test_concurrent_equivalent_jobs_join(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state.db")
    ready = Barrier(2)

    def submit(name: str) -> tuple[Job, bool]:
        ready.wait(timeout=5)
        return store.submit(name)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.map(submit, ["Owner/Repo", "owner/repo"])
    assert first[0] == second[0]
    assert first[1] != second[1]
    assert store.submit("owner/repo", "release")[1]


def test_failed_publish_rolls_back_inserted_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    job, _ = store.submit("owner/repo")
    store.start(job.id)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("""
            CREATE TRIGGER fail_publish BEFORE UPDATE OF status ON jobs
            WHEN NEW.status = 'completed' BEGIN SELECT RAISE(ABORT, 'disk failure'); END;
        """)
    with pytest.raises(sqlite3.IntegrityError, match="disk failure"):
        store.complete(job.id, SHA)
    assert store.snapshots("owner/repo") == []
    assert store.job(job.id).status == "running"


def test_recovery_keeps_snapshots_and_queued_work(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = StateStore(path)
    previous, _ = store.submit("owner/repo")
    store.start(previous.id)
    store.complete(previous.id, SHA)
    interrupted, _ = store.submit("owner/repo")
    store.start(interrupted.id)
    pending, _ = store.submit("owner/repo", "release")
    reopened = StateStore(path)
    assert reopened.job(interrupted.id).status == "running"
    assert reopened.recover_interrupted() == [pending.id]
    assert reopened.job(interrupted.id).status == "failed"
    assert reopened.job(interrupted.id).error == "Interrupted during analysis"
    assert reopened.job(previous.id).sha == reopened.snapshots("owner/repo")[0].sha
    retry, created = reopened.submit("owner/repo")
    assert created
    reopened.fail(retry.id, "Temporary\n  failure")
    assert reopened.job(retry.id).error == "Temporary failure"
    with pytest.raises(KeyError):
        reopened.job(-1)
