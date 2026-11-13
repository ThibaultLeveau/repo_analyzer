import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Event

from test_ask import CitedModel
from test_manifests import manifest_snapshot
from test_scoring import ticket

from repopilot.grounding import Answer, Claim
from repopilot.python_facts import analyze_python
from repopilot.scoring import Assessment, Scores, assess


def test_revisions_survive_restart_and_legacy_rows(tmp_path: Path) -> None:
    src = manifest_snapshot(tmp_path / "sources", {"app.py": "value = 1"})
    first, second = "a" * 40, "b" * 40
    issue = ticket(1, "Change app.py")
    facts = analyze_python(src)
    original = assess(src, facts, "o/r", first, issue, CitedModel())
    scores = Scores(tmp_path)
    scores.save("o/r", first, issue, original)
    # Previous installations have immutable score rows without evaluation metadata.
    with closing(sqlite3.connect(scores.path)) as db, db:
        db.execute("DROP TABLE score_details")
    scores = Scores(tmp_path)
    edited = issue.model_copy(update={"body": "Clarify the behavior."})
    updated = assess(src, facts, "o/r", second, edited, CitedModel())
    scores.save("o/r", second, issue, updated)
    scores.save("o/r", second, edited, updated)
    wording = Answer("ok", (Claim("Different wording.", updated.evidence),))
    new_rubric = replace(updated, rubric="next", reasoning=wording)
    scores.evaluate("o/r", second, edited, lambda: new_rubric, rubric="next")
    newest, body_edit, sha_edit, legacy = Scores(tmp_path).history("O/R", issue.id)
    assert newest.score.rubric == "next" and body_edit.score.rubric != "next"
    assert (
        not newest.changes(body_edit)
        and newest.score.reasoning != body_edit.score.reasoning
    )
    assert sha_edit.issue_revision != body_edit.issue_revision
    assert legacy.sha == first and sha_edit.sha == second
    assert legacy.title is None and legacy.score == original
    assert newest.title == edited.title and newest.evaluated_at


def test_concurrent_submissions_compute_only_once(tmp_path: Path) -> None:
    issue = ticket(2, "Unknown")
    scores = Scores(tmp_path)
    started, release = Event(), Event()
    calls = []

    def compute() -> Assessment:
        calls.append(1)
        started.set()
        assert release.wait(5)
        return Assessment(None, "Unknown", (), (), (), Answer("insufficient_evidence"))

    def run() -> None:
        scores.evaluate("o/r", "a" * 40, issue, compute)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(run)
        assert started.wait(5)
        second = pool.submit(run)
        release.set()
        first.result(5)
        second.result(5)
    run()
    assert calls == [1] and len(scores.history("o/r", issue.id)) == 1
