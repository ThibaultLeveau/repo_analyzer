import json
from pathlib import Path

import pytest
from test_manifests import manifest_snapshot

from repopilot.audit import Audit, analyze_audit
from repopilot.dependencies import analyze_dependencies
from repopilot.python_facts import analyze_python


def audit_files(root: Path, files: dict[str, str]) -> Audit:
    snapshot = manifest_snapshot(root, files)
    facts = analyze_python(snapshot)
    report = analyze_audit(snapshot, facts, analyze_dependencies(snapshot, facts=facts))
    for item in report.findings:
        lines = snapshot.lines(item.span.path)
        assert 1 <= item.span.start <= item.span.end <= len(lines)
        assert item.explanation and item.limit
    return report


def test_three_rules_report_stable_ranged_evidence(tmp_path: Path) -> None:
    files = {
        "src/tools.py": "# tools\nfrom yaml import dump\ndef Write(data):\n    return dump(data)\n",
        "requirements.txt": "packaging\n",
        "tests/test_other.py": "",
    }
    report = audit_files(tmp_path / "one", files)
    assert report == audit_files(tmp_path / "two", dict(reversed(files.items())))
    assert [f.rule for f in report.findings] == ["DEP001", "STYLE001", "TEST001"]
    assert [(f.span.start, f.span.end) for f in report.findings] == [
        (2, 2),
        (3, 4),
        (3, 4),
    ]
    assert len({f.key for f in report.findings}) == 3
    assert report.test_files == 1 and report.coverage is None


def test_matching_filename_measured_coverage_and_declared_dependency(
    tmp_path: Path,
) -> None:
    files = {
        "area.py": "import yaml\ndef write(): pass\n",
        "tests/area_test.py": "def test_write(): pass",
        "requirements.txt": "PyYAML",
        "coverage.json": json.dumps(
            {"meta": {"format": 3}, "totals": {"covered_lines": 1, "num_statements": 4}}
        ),
    }
    report = audit_files(tmp_path, files)
    assert not report.findings and report.test_files == 1
    assert report.coverage == (1, 4)


@pytest.mark.parametrize(
    "coverage",
    [
        "[]",
        "not json",
        '{"meta":{"format":3},"totals":{"covered_lines":8,"num_statements":1}}',
    ],
)
def test_inconclusive_inputs(tmp_path: Path, coverage: str) -> None:
    files = {
        "bad.py": "def Bad(:",
        "stub.py": "import cv2\n",
        "requirements.txt": "-r unavailable.txt\n",
        "coverage.json": coverage,
    }
    report = audit_files(tmp_path, files)
    assert (
        not report.findings and report.skipped_modules == 1 and report.coverage is None
    )
