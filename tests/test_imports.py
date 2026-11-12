import io
import time
import zipfile
from pathlib import Path
from threading import Event

import httpx2
import pytest
from filelock import Timeout

from repopilot.imports import import_service
from repopilot.models import RepositoryHead
from repopilot.state import Job, StateStore

URL = "https://github.com/old/project"
HEAD = RepositoryHead("New/Project", "stable", "a" * 40)


class Remote:
    def __init__(self) -> None:
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("project/README.md", "# Hello")
        self.zip_bytes = data.getvalue()
        self.calls: list[str] = []
        self.downloading = Event()
        self.proceed = Event()
        self.proceed.set()
        self.fail_download = False

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request.url.path)
        if request.url.host == "codeload.github.com":
            self.downloading.set()
            assert self.proceed.wait(timeout=10)
            if self.fail_download:
                return httpx2.Response(503)
            return httpx2.Response(200, stream=httpx2.ByteStream(self.zip_bytes))
        if request.url.path == "/repos/old/project":
            return httpx2.Response(
                200,
                json={
                    "full_name": HEAD.full_name,
                    "private": False,
                    "default_branch": HEAD.default_branch,
                },
            )
        if request.url.path == "/repos/New/Project/commits/stable":
            return httpx2.Response(200, json={"sha": HEAD.sha})
        return httpx2.Response(404)


def terminal(store: StateStore, job_id: int) -> Job:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = store.job(job_id)
        if job.status in {"completed", "failed"}:
            return job
        time.sleep(0.01)
    pytest.fail("Import did not reach a terminal state")


def test_async_import_persists_progress_and_joins_active_job(tmp_path: Path) -> None:
    remote = Remote()
    remote.proceed.clear()
    with import_service(
        tmp_path, transport=httpx2.MockTransport(remote.respond)
    ) as service:
        job = service.submit(URL)
        try:
            assert remote.downloading.wait(timeout=10)
            assert service.submit(URL).id == job.id
            assert service.store.job(job.id).status == "running"
            assert service.store.source(job.id) == (HEAD, False)
            with pytest.raises(Timeout), import_service(tmp_path):
                pytest.fail("A second worker acquired the same data directory")
            assert service.store.job(job.id).status == "running"
        finally:
            remote.proceed.set()
        done = terminal(service.store, job.id)
        assert (done.status, done.repository, done.sha) == (
            "completed",
            HEAD.full_name,
            HEAD.sha,
        )
        assert service.store.source(job.id) == (HEAD, True)
        assert (
            service.cache.open(HEAD.full_name, HEAD.sha).read_text("README.md")
            == "# Hello"
        )
    assert len(remote.calls) == 3


def test_retry_preserves_resolved_sha_across_restart(tmp_path: Path) -> None:
    remote = Remote()
    remote.fail_download = True
    with import_service(
        tmp_path, transport=httpx2.MockTransport(remote.respond)
    ) as service:
        failed = terminal(service.store, service.submit(URL).id)
        assert failed.status == "failed"
        assert failed.error and "archive" in failed.error
        assert not service.store.snapshots(HEAD.full_name)
    remote.calls.clear()
    remote.fail_download = False
    with import_service(
        tmp_path, transport=httpx2.MockTransport(remote.respond)
    ) as restarted:
        retry = terminal(restarted.store, restarted.retry(failed.id).id)
        assert retry.id != failed.id and retry.sha == HEAD.sha
        assert remote.calls == [f"/{HEAD.full_name}/zip/{HEAD.sha}"]
        assert restarted.store.job(failed.id).status == "failed"
        unavailable = terminal(
            restarted.store, restarted.submit("https://github.com/deleted/project").id
        )
        assert unavailable.error and "unavailable" in unavailable.error
        assert restarted.store.snapshots(HEAD.full_name)[0].sha == HEAD.sha


def test_restart_dispatches_queued_jobs_and_marks_interrupted_runs(
    tmp_path: Path,
) -> None:
    store = StateStore(tmp_path / "state.db")
    pending, _ = store.submit("old/project")
    interrupted, _ = store.submit("other/project")
    store.start(interrupted.id)
    remote = Remote()
    with import_service(
        tmp_path, transport=httpx2.MockTransport(remote.respond)
    ) as service:
        assert terminal(service.store, pending.id).sha == HEAD.sha
        assert service.store.job(interrupted.id).error == "Interrupted during analysis"
