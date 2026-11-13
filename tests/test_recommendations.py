from pathlib import Path

from test_manifests import manifest_snapshot
from test_scoring import ticket

from repopilot.history import HistoryLink, HistoryRecord, Observation
from repopilot.python_facts import analyze_python
from repopilot.recommendations import recommend

REPO, SHA = "owner/repo", "a" * 40
FILES = {
    "src/loader.py": "def parse(data):\n    return data\n",
    "tests/test_loader.py": "from loader import parse\n",
    "src/other.py": "def helper(): pass\n",
}


def history() -> Observation:
    closed = HistoryRecord(
        kind="issue",
        id="301",
        number=3,
        state="closed",
        url="https://github.com/owner/repo/issues/3",
        title="Handle catalog validation in src/loader.py",
        body="Improve catalog validation",
        author="maintainer",
        timestamp="2026-01-01",
        updated_at="2026-01-04",
        labels=("bug",),
    )
    change = closed.model_copy(
        update={
            "kind": "pull",
            "id": "411",
            "number": 11,
            "state": "merged",
            "url": "https://github.com/owner/repo/pull/11",
            "paths": ("src/loader.py",),
        }
    )
    return Observation(
        repository=REPO,
        sha=SHA,
        issue=3,
        observed_at=100,
        records=[closed, change],
        links=[
            HistoryLink(source=change.key, relation="references", target=closed.key)
        ],
    )


def test_recommendations_are_reproducible_and_separate_weak_candidates(
    tmp_path: Path,
) -> None:
    snapshot = manifest_snapshot(tmp_path / "sources", FILES)
    facts = analyze_python(snapshot)
    a = ticket(4, "Handle catalog validation in src/loader.py")
    b = ticket(5, a.title)
    c = ticket(8, "Update catalog validation in src/other.py")
    d = ticket(9, a.title).model_copy(
        update={"labels": ["good first issue", "blocked"]}
    )
    e = ticket(10, "Update catalog validation")
    f = ticket(12, "Improve unrelated icon")
    candidates = [d, c, b, f, e, a]
    batch = history()
    ranked = recommend(snapshot, facts, REPO, SHA, candidates, [batch])
    assert ranked == recommend(
        snapshot, facts, REPO, SHA, list(reversed(candidates)), [batch]
    )
    assert [r.issue.number for r in ranked.recommended] == [4, 5]
    item = ranked.recommended[0]
    assert item.precedent.issue.number == 3 and item.confidence == "High"
    assert item.assessment.score == 25 and item.overlap == ["src/loader.py"]
    assert [e.span.path for e in item.assessment.evidence] == [
        "src/loader.py",
        "tests/test_loader.py",
    ]
    assert [i.number for i, _ in ranked.uncertain] == [8, 9, 10, 12]
    assert "blocked" in ranked.uncertain[1][1]
    assert not recommend(snapshot, facts, REPO, SHA, [a], []).recommended
    assert not recommend(snapshot, facts, "other/repo", SHA, [a], [batch]).recommended
    batch.records[1].state = "closed"
    assert not recommend(snapshot, facts, REPO, SHA, [a], [batch]).recommended
