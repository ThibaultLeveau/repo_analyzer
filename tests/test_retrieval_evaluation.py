from pathlib import Path

import pytest
from evaluate_retrieval import CORPUS, Corpus, evaluate, measure, regressions

from repopilot.python_facts import Span


def test_metrics_use_unique_reference_ranges_and_ranked_cutoff() -> None:
    a, b, c = Span("a.py", 2, 4), Span("a.py", 10, 10), Span("b.py", 1, 1)
    expected = [a, a, b, c]
    tied = [Span("a.py", 1, 2), Span("a.py", 4, 6), c]
    first = measure(expected, tied, 2)
    assert first.recall == pytest.approx(1 / 3) and first.diversity == 0.5
    assert measure(expected, [c, *tied], 2).recall == pytest.approx(2 / 3)
    empty = measure(expected, [], 2)
    assert empty.recall == empty.diversity == 0
    with pytest.raises(ValueError):
        measure([], [], 0)


def test_quality_gate_detects_loss_of_a_reference_hit(tmp_path: Path) -> None:
    corpus = Corpus.model_validate_json(CORPUS.read_bytes())
    sha, rows = evaluate(corpus, tmp_path)
    assert sha == corpus.sha and set(rows) == {"lexical", "vector", "hybrid"}
    assert not regressions(rows, corpus)
    query = corpus.queries[0]
    rows["hybrid"][query.id] = measure(query.expected, [], corpus.k)
    assert regressions(rows, corpus) == ["hybrid/symbol"]
