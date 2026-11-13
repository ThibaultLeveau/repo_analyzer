import json
from pathlib import Path
from typing import Any

import pytest
from evaluate_retrieval import CORPUS, Corpus, evaluate
from langchain_core.messages import BaseMessage
from test_ask import CitedModel
from test_manifests import manifest_snapshot

from repopilot.ask import Ask
from repopilot.events import EventStore
from repopilot.grounding import Evidence
from repopilot.python_facts import Span
from repopilot.rerank import EXCERPT_CHARS, MAX_CANDIDATES, Reranker
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.vectors import VectorIndex


class RankModel:
    def __init__(self, mode: str = "reverse"):
        self.mode = mode
        self.requests: list[dict[str, Any]] = []

    def invoke(self, messages: list[BaseMessage]) -> object:
        raw = messages[-1].content
        assert isinstance(raw, str)
        request = json.loads(raw)
        self.requests.append(request)
        ids = [item["id"] for item in request["candidates"]]
        if self.mode == "timeout":
            raise TimeoutError
        if self.mode == "invalid":
            return {"ids": [ids[-1], "invented"]}
        if self.mode == "duplicate":
            return {"ids": [ids[-1], ids[-1]]}
        return {"ids": ids if self.mode == "tie" else list(reversed(ids))}


def test_bounded_ranking_fallback_ties_and_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOPILOT_RERANK", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-key")
    assert Reranker.configured() is None
    monkeypatch.setenv("REPOPILOT_RERANK", "1")
    assert Reranker.configured()
    monkeypatch.delenv("OPENAI_API_KEY")
    assert Reranker.configured() is None
    candidates = [
        Evidence(
            "a/b",
            "a" * 40,
            Span(f"file{i}.py", 1, 1),
            "x" * EXCERPT_CHARS + "private-suffix",
        )
        for i in range(MAX_CANDIDATES + 2)
    ]
    model = RankModel()
    ranker = Reranker(model)
    ticks = iter(range(90))
    store = EventStore(tmp_path, clock=lambda: float(next(ticks)))
    with store.scope("a/b", "a" * 40):
        assert not ranker.rank("test", [])
        assert ranker.rank("test", candidates[:1]) == candidates[:1]
        assert not model.requests
        ordered = ranker.rank("?" * 2000, candidates)
        assert (
            ordered
            == list(reversed(candidates[:MAX_CANDIDATES])) + candidates[MAX_CANDIDATES:]
        )
        sent = model.requests[-1]
        assert (
            len(sent["question"]) == 1000 and len(sent["candidates"]) == MAX_CANDIDATES
        )
        assert all(
            set(row) == {"id", "excerpt"} and len(row["excerpt"]) <= EXCERPT_CHARS
            for row in sent["candidates"]
        )
        assert "private-suffix" not in repr(sent)
        for mode in ("tie", "invalid", "timeout"):
            model.mode = mode
            assert ranker.rank("test", candidates) == candidates
        model.mode = "duplicate"
        assert ranker.rank("test", [candidates[0], *candidates[0:4]]) == [
            candidates[3],
            *candidates[:3],
        ]
    events = store.recent()
    assert len(events) == 5 and all(
        e.operation == "model.rerank" and e.duration_ms == 1000 for e in events
    )
    assert sum(not e.metrics.success for e in events) == 2
    assert b"private-suffix" not in store.path.read_bytes()


def test_ask_reranks_before_answer_and_restores_baseline_on_timeout(
    tmp_path: Path,
) -> None:
    repo, sha = "owner/repo", "a" * 40
    cache = SnapshotCache(tmp_path / "sources")
    manifest_snapshot(cache.root, {f"module{i}.py": f"item = {i}" for i in range(9)})
    state = StateStore(tmp_path / "state.db")
    vector = VectorIndex(tmp_path, cache)
    model = RankModel()
    ask = Ask(cache, state, vector, CitedModel(), reranker=Reranker(model))

    def first(service: Ask) -> str:
        saved = service.run(repo, sha, "item")
        return service.saved(repo, sha, saved)[1].claims[0].sources[0].span.path

    baseline = first(Ask(cache, state, vector, CitedModel()))
    assert first(ask) != baseline and len(model.requests) == 1
    model.mode = "timeout"
    assert first(ask) == baseline


def test_comparison_counts_calls_on_identical_questions(tmp_path: Path) -> None:
    corpus = Corpus.model_validate_json(CORPUS.read_bytes())
    _, rows = evaluate(corpus, tmp_path, compare_rerank=True)
    assert (
        rows["hybrid"].keys()
        == rows["reranked"].keys()
        == {q.id for q in corpus.queries}
    )
    for mode in ("hybrid", "reranked"):
        for result in rows[mode].values():
            assert (
                0 <= result.recall <= 1
                and 0 <= result.diversity <= 1
                and result.elapsed_ms >= 0
            )
            assert result.rerank_calls == int(mode == "reranked")
