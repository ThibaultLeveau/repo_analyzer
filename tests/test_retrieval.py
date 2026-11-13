from dataclasses import replace
from pathlib import Path

import pytest
from test_manifests import manifest_snapshot
from test_vectors import REPO, SHA, Vectors

from repopilot.retrieval import CONTEXT_CHARS, SOURCE_LIMIT, HybridRetriever
from repopilot.search import chunks
from repopilot.snapshots import SnapshotCache
from repopilot.vectors import Neighbor, VectorIndex


def test_fusion_deduplicates_channels_and_isolates_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = SnapshotCache(tmp_path)
    snapshot = manifest_snapshot(
        tmp_path,
        {"a.py": "def fetch(): pass", "b.py": "fetch()", "c.py": "def query(): pass"},
    )
    a, b, c = chunks(snapshot, REPO, SHA)
    index = VectorIndex(tmp_path, cache, Vectors())
    hits = [
        Neighbor(b, 0.2),
        Neighbor(c, 0.1),
        Neighbor(a, 0.2),
        Neighbor(replace(c, sha="f" * 40), 0),
    ]
    monkeypatch.setattr(index, "nearest", lambda *args: hits)
    retriever = HybridRetriever(cache, index)
    result = retriever.retrieve(REPO, SHA, "fetch")
    assert [m.chunk for m in result.matches] == [a, b, c]
    assert [h.channel for h in result.hits()] == [
        "Lexical + Semantic",
        "Lexical + Semantic",
        "Semantic",
    ]
    assert result.hits()[0].label == "a.fetch"
    hits.reverse()
    assert retriever.retrieve(REPO, SHA, "fetch") == result
    hit, *rest = retriever.retrieve(REPO, SHA, "get the records").matches
    assert hit.chunk == c and [m.chunk for m in rest] == [a, b]
    assert hit.hit.channel == "Semantic"
    hits.clear()
    lexical = retriever.retrieve(REPO, SHA, "fetch")
    assert lexical.hits()[0].label == "a.fetch"
    assert [e.span.path for e in lexical.evidence()] == ["a.py", "b.py"]


def test_bound_evidence_and_keep_exact_file_navigation(tmp_path: Path) -> None:
    cache = SnapshotCache(tmp_path)
    manifest_snapshot(
        tmp_path,
        {**{f"r{i}.txt": "catalog " * 195 for i in range(20)}, "empty.txt": ""},
    )
    retrieval = HybridRetriever(cache, VectorIndex(tmp_path, cache, Vectors()))
    evidence = retrieval.retrieve(REPO, SHA, "catalog").evidence()
    assert 1 <= len(evidence) <= SOURCE_LIMIT
    assert sum(len(s.excerpt) for s in evidence) <= CONTEXT_CHARS
    assert retrieval.retrieve(REPO, SHA, "empty.txt").hits()[0].kind == "File"
