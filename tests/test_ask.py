import json
import sqlite3
from pathlib import Path
from threading import Event

import pytest
from langchain_core.messages import BaseMessage
from test_manifests import manifest_snapshot
from test_vectors import Vectors

from repopilot.ask import Ask
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.vectors import EmbeddingConfig, VectorIndex

REPO = "owner/repo"
SHA = "a" * 40


class CitedModel:
    def __init__(self) -> None:
        self.calls = 0
        self.mode = "ok"
        self.wait: Event | None = None

    def invoke(self, messages: list[BaseMessage]) -> object:
        self.calls += 1
        if self.wait is not None:
            assert self.wait.wait(timeout=10)
        if self.mode == "timeout":
            raise TimeoutError
        payload = messages[-1].content
        assert isinstance(payload, str)
        sources = json.loads(payload)["evidence"]
        if self.mode == "unrelated":
            return {"claims": []}
        cite = sources[0]["id"] if self.mode == "ok" else "wrong-id"
        return {
            "claims": [{"text": "The source stores a value.", "evidence_ids": [cite]}]
        }


def test_questions_validate_citations_and_persist_by_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = SnapshotCache(tmp_path / "sources")
    manifest_snapshot(
        cache.root, {"src/store.py": "def store_value():\n    return 'database'\n"}
    )
    state = StateStore(tmp_path / "state.db")
    model = CitedModel()
    vectors = VectorIndex(tmp_path, cache, Vectors(), EmbeddingConfig("fixture", 2))
    ask = Ask(cache, state, vectors, model)
    question = "Where is store_value defined?"
    saved_id = ask.run(REPO, SHA, question, "src/store.py")
    original, answer = ask.saved(REPO, SHA, saved_id)
    assert original == question and answer.status == "ok"
    (source,) = answer.claims[0].sources
    assert source.span.path == "src/store.py" and source.excerpt.startswith(
        "def store_value("
    )
    assert source.url.endswith("&line=1&end=2#L1")
    with sqlite3.connect(state.path) as db:
        row = db.execute(
            "SELECT citations FROM questions WHERE id=?", (saved_id,)
        ).fetchone()
        assert json.loads(row[0]) == [source.id]
    reopened = Ask(cache, StateStore(state.path), vectors, model)
    assert (
        reopened.saved(REPO, SHA, saved_id) == (original, answer) and model.calls == 1
    )
    for repo, sha in ((REPO, "b" * 40), ("owner/other", SHA)):
        with pytest.raises(KeyError):
            reopened.saved(repo, sha, saved_id)
    unrelated = ask.run(REPO, SHA, "astronaut helmets")
    assert (
        ask.saved(REPO, SHA, unrelated)[1].status == "insufficient_evidence"
        and model.calls == 1
    )

    assert vectors.build(REPO, SHA) == 1
    for mode, expected in (
        ("ok", "ok"),
        ("unrelated", "insufficient_evidence"),
        ("invalid", "invalid_response"),
        ("timeout", "timeout"),
    ):
        model.mode = mode
        saved_id = ask.run(REPO, SHA, "Where are the records kept?")
        assert ask.saved(REPO, SHA, saved_id)[1].status == expected
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    offline = Ask(cache, state, VectorIndex(tmp_path, cache), None)
    saved_id = offline.run(REPO, SHA, question)
    assert offline.saved(REPO, SHA, saved_id)[1].status == "missing_key"
    for question in (" ", "a" * 1001):
        with pytest.raises(ValueError):
            offline.run(REPO, SHA, question)
