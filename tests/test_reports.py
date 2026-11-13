import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import TypeAdapter
from test_manifests import manifest_snapshot
from test_scoring import ticket
from test_vectors import REPO, SHA

from repopilot.architecture import Architecture
from repopilot.artifacts import Artifacts
from repopilot.grounding import Answer, Claim, Evidence
from repopilot.python_facts import Span
from repopilot.reports import build_report
from repopilot.scoring import Assessment, Scores
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore


def test_export_only_reads_saved_data_and_renders_all_formats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = SnapshotCache(tmp_path / "sources")
    snapshot = manifest_snapshot(
        cache.root,
        {
            "main.py": '"""Read values."""\nimport yaml\ndef read(): pass',
            "requirements.txt": "PyYAML",
            "README.md": "## Installation\n```sh\npython -m pip install -r requirements.txt\n```",
        },
    )
    state = StateStore(tmp_path / "state.db")
    architecture = Architecture(cache, state)
    scores = Scores(tmp_path)
    empty = build_report(architecture, scores, REPO, SHA)
    assert (
        empty.sections["Architecture"] is None
        and empty.sections["Questions and answers"] == []
    )
    assert not (snapshot.database.parent / "analysis.db").exists()
    architecture.artifacts.prepare(snapshot)
    architecture.audit(REPO, SHA)
    source = Evidence(REPO, SHA, Span("main.py", 1, 1), '"""Read values."""')
    answer = Answer(
        "ok",
        (Claim("<script>alert(1)</script> [click](https://example.org)", (source,)),),
    )
    state.save_question(
        REPO,
        SHA,
        "What does main do?",
        TypeAdapter(Answer).dump_json(answer).decode(),
        [source.id],
    )
    issue = ticket(123, "Change main.py")
    scores.save(
        REPO, SHA, issue, Assessment(25, "High", ("main",), (), (source,), answer)
    )
    for method in ("prepare", "python", "dependencies", "setup"):
        monkeypatch.setattr(
            Artifacts, method, lambda *args: pytest.fail("Unexpected analysis")
        )
    report = build_report(architecture, scores, REPO, SHA)
    assert all(report.sections.values())
    assert (
        report.sections["Questions and answers"]
        and report.sections["Questions and answers"][0].timestamp
    )
    assert (
        report.sections["Issue assessments"]
        and report.sections["Issue assessments"][0].timestamp
    )
    rendered = report.render("html")
    assert "<script>" not in rendered and 'href="https://example.org"' not in rendered
    assert f"https://github.com/{REPO}/blob/{SHA}/main.py#L1-L1" in rendered
    payload = json.loads(report.render("json"))
    assert (
        payload["schema_version"] == 1
        and payload["repository"] == REPO
        and payload["sha"] == SHA
    )
    # Normalize wall-clock metadata before comparing the complete rendered files.
    for records in report.sections.values():
        if records:
            records[:] = [
                replace(
                    row, timestamp="2026-01-01T12:00:00Z" if row.timestamp else None
                )
                for row in records
            ]
    golden = {
        "empty": {
            "json": "d046ff782f146ca732670177271eff4aef22025791520bb27236759e70cb31d1",
            "md": "e5ad02b45978092e10935d86fd85902dca81598d8b11064b2e61df7266a67c6c",
            "html": "4287394513ac8056c325d85ed2d2137616529d902a85e49df1e090378ae427d0",
        },
        "populated": {
            "json": "8cd0a91e7238b1fa18f508d26096dab84f2a9d6ed1777c9b0bab9a2edd8d3e86",
            "md": "e16813b105395fef48a16042d54b34ac59b82fa4b52ea8170b1494a3a2f4b765",
            "html": "eaefdc241d1be52d70e9b3ccd191c3514a6cf023d424879385297d69b07ee118",
        },
    }
    actual = {
        name: {
            ext: hashlib.sha256(item.render(ext).encode()).hexdigest()
            for ext in ("json", "md", "html")
        }
        for name, item in (("empty", empty), ("populated", report))
    }
    assert actual == golden, json.dumps(actual)
