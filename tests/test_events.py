from functools import partial
from itertools import count
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from openai import DefaultHttpxClient
from test_imports import HEAD, URL, Remote, terminal

from repopilot.app import create_app
from repopilot.events import EventStore, measure
from repopilot.imports import import_service
from repopilot.state import StateStore
from repopilot.vectors import EmbeddingConfig, OpenAIEmbedder, VectorError


def test_import_attempts_share_job_and_snapshot_after_restart(tmp_path: Path) -> None:
    remote = Remote()
    remote.fail_download = True
    with import_service(
        tmp_path,
        transport=httpx2.MockTransport(remote.respond),
        clock=count(1).__next__,
    ) as service:
        failed = terminal(service.store, service.submit(URL).id)
        remote.fail_download = False
        done = terminal(service.store, service.retry(failed.id).id)
    store = EventStore(tmp_path)
    fetches = [e for e in store.recent(HEAD.full_name) if e.operation == "import.fetch"]
    assert [(e.job_id, e.sha, e.metrics.success) for e in fetches] == [
        (done.id, HEAD.sha, True),
        (failed.id, HEAD.sha, False),
    ]
    assert all(e.duration_ms >= 1000 for e in fetches)
    summary = store.totals(HEAD.full_name)
    assert summary.failures == summary.retries == 1
    assert summary.reported_tokens is None
    assert store.recent(since=10**6) == []


def test_retention_keeps_jobs_and_filters_windows(tmp_path: Path) -> None:
    jobs = StateStore(tmp_path / "state.db")
    active, _ = jobs.submit("one/two")
    jobs.start(active.id)
    store = EventStore(tmp_path, clock=count().__next__, limit=3)
    for repo in ["one/two", "one/two", "three/four", "one/two"]:
        with store.scope(repo, job_id=active.id), measure("cache.read") as metrics:
            metrics.cache = "hit"
    reopened = EventStore(tmp_path)
    assert len(reopened.recent()) == 3
    assert reopened.totals("One/Two", since=4).cache_hits == 1
    assert jobs.job(active.id).status == "running"


def test_http_actions_and_cache_discard_user_content(
    tmp_path: Path, explorer_url: str
) -> None:
    root = explorer_url.removesuffix("/code")
    private = "private-user-question"
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        assert client.get(root + "/search", params={"q": private}).is_success
        assert client.get(root + "/dependencies").is_success
        assert (
            client.post(
                root + "/ask", data={"csrf_token": private, "question": private}
            ).status_code
            == 403
        )
        assert client.get(root.replace("Demo", private) + "/code").status_code == 404
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as reopened:
        assert reopened.get(root + "/dependencies").is_success
    store = EventStore(tmp_path)
    totals = store.totals("Owner/Demo")
    assert totals.cache_hits and totals.cache_misses
    assert totals.failures == 1
    events = store.recent("Owner/Demo")
    assert {"retrieval.search", "web.ask_question", "web.search_view"} <= {
        e.operation for e in events
    }
    assert all(e.sha == "a" * 40 for e in events)
    raw = store.path.read_bytes()
    assert (
        private.encode() not in raw and b"# original" not in raw and b"msg =" not in raw
    )


def test_embedding_usage_and_terminal_provider_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = "private-fixture-key"
    body = "private-embedding-text"
    responses = iter(
        [
            httpx2.Response(
                200,
                json={
                    "object": "list",
                    "model": "fixture",
                    "data": [{"object": "embedding", "index": 0, "embedding": [2.0]}],
                    "usage": {"prompt_tokens": 8, "total_tokens": 8},
                },
            ),
            httpx2.Response(401, json={"error": {"message": key}}),
        ]
    )
    monkeypatch.setenv("OPENAI_API_KEY", key)
    monkeypatch.setattr(
        "repopilot.events.DefaultHttpxClient",
        partial(
            DefaultHttpxClient,
            transport=httpx2.MockTransport(lambda _: next(responses)),
        ),
    )
    store = EventStore(tmp_path)
    with store.scope("owner/repo", "a" * 40):
        embedder = OpenAIEmbedder()
        assert embedder.embed([body], EmbeddingConfig("fixture", 1)) == [[2.0]]
        with pytest.raises(VectorError):
            embedder.embed([body], EmbeddingConfig("fixture", 1))
    totals = store.totals()
    assert (
        totals.openai_calls,
        totals.reported_tokens,
        totals.calls_without_usage,
        totals.provider_failures,
    ) == (2, 8, 1, 1)
    assert (
        key.encode() not in store.path.read_bytes()
        and body.encode() not in store.path.read_bytes()
    )
