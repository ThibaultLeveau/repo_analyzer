from dataclasses import replace
from pathlib import Path

import pytest
from test_ask import CitedModel
from test_manifests import manifest_snapshot

from repopilot.issues import Issue
from repopilot.python_facts import analyze_python
from repopilot.scoring import Scores, assess, easier_first


def ticket(number: int, title: str) -> Issue:
    return Issue(
        id=number,
        number=number,
        title=title,
        body="",
        labels=[],
        created_at="2026-10-01",
        updated_at="2026-10-02",
    )


def test_factors_citations_ambiguity_and_storage(tmp_path: Path) -> None:
    files = {
        "src/alpha.py": "def parse(): pass\n",
        "tests/test_alpha.py": "import alpha\n",
        "x/api.py": "a = 1",
        "y/api.py": "b = 2",
    }
    snapshot = manifest_snapshot(tmp_path / "sources", files)
    facts = analyze_python(snapshot)
    model = CitedModel()
    model.mode = "bad-id"
    first = ticket(2, "Update alpha.parse")
    score = assess(snapshot, facts, "owner/repo", "a" * 40, first, model)
    assert (score.score, score.difficulty, score.confidence) == (25, "Low", "High")
    assert score.reasoning.status == "invalid_response" and not score.reasoning.claims
    assert [e.span.path for e in score.evidence] == [
        "src/alpha.py",
        "tests/test_alpha.py",
    ]
    scores = Scores(tmp_path)
    scores.save("Owner/Repo", "a" * 40, first, score)
    assert Scores(tmp_path).current("owner/repo", "a" * 40, (first,)) == {2: score}
    edited = first.model_copy(update={"body": "changed"})
    assert not scores.current("Owner/Repo", "a" * 40, (edited,))
    second = ticket(1, "api.py")
    ambiguous = assess(snapshot, facts, "owner/repo", "a" * 40, second, model)
    assert (
        ambiguous.score is None
        and "Ambiguous names: api.py" in ambiguous.factors[0].reason
    )
    assert model.calls == 1
    assert easier_first([first, second], {1: score, 2: score}) == [second, first]
    assert easier_first([second, first], {1: ambiguous, 2: score}) == [first, second]


@pytest.mark.parametrize(
    ("points", "band"), [(34, "Low"), (35, "Moderate"), (64, "Moderate"), (65, "High")]
)
def test_thresholds_and_missing_test_evidence(
    tmp_path: Path, points: int, band: str
) -> None:
    snapshot = manifest_snapshot(tmp_path, {"core.py": "pass\n" * 51})
    result = assess(
        snapshot,
        analyze_python(snapshot),
        "o/r",
        "a" * 40,
        ticket(1, "core.py"),
        CitedModel(),
    )
    assert result.score == 60 and result.confidence == "Moderate"
    assert result.factors[2].points == 20
    assert replace(result, score=points).difficulty == band
