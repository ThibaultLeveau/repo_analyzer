import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from test_manifests import manifest_snapshot

from repopilot.languages import analyze_snapshot
from repopilot.maintenance import maintenance_findings, maintenance_fix

SHA = "a" * 40
SOURCE = '''def collect(value, items=[]):
    """If items is omitted or None, create a fresh list for each call."""
    items.append(value)
    return items
'''
REGRESSION = """from collect import collect
assert collect(1) == [1]
assert collect(2) == [2]
assert collect(3, None) == [3]
supplied = [4]
assert collect(5, supplied) is supplied and supplied == [4, 5]
"""


def test_validated_patch_fixes_only_the_trusted_fixture(tmp_path: Path) -> None:
    snap = manifest_snapshot(tmp_path / "sources", {"collect.py": SOURCE})
    module = analyze_snapshot(snap).modules[0]
    (item,) = maintenance_findings(snap, module)
    patch = maintenance_fix(snap, module, item, SHA)
    assert patch.evidence == item.span and (item.span.start, item.span.end) == (1, 1)
    assert maintenance_fix(snap, module, item, SHA, proposed=patch.diff) == patch
    for invalid in (
        patch.diff.replace("b/collect.py", "b/other.py"),
        patch.diff.replace("@@ -1", "@@ -7"),
        patch.diff.replace("items = []", "items = [0]"),
    ):
        assert not maintenance_fix(snap, module, item, SHA, proposed=invalid).diff
    assert not maintenance_fix(snap, module, item, "b" * 40).diff
    assert not maintenance_fix(snap, module, replace(item, title="unrelated"), SHA).diff
    (tmp_path / "collect.py").write_bytes(SOURCE.encode())
    (tmp_path / "regression.py").write_bytes(REGRESSION.encode())
    command = [sys.executable, "-B", "regression.py"]
    before = subprocess.run(command, cwd=tmp_path, capture_output=True, check=False)
    assert before.returncode == 1 and b"AssertionError" in before.stderr
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "-"],
        cwd=tmp_path,
        input=patch.diff.encode(),
        check=True,
    )
    subprocess.run(command, cwd=tmp_path, check=True)
    assert snap.read_text("collect.py") == SOURCE


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace(
            "If items is omitted or None, create a fresh list for each call.",
            "Collect values.",
        ),
        SOURCE.replace("If items", "Cache results. If items"),
        SOURCE.replace("items=[]", "items=[1]"),
        SOURCE.replace("items=[]", "items: list[int]=[]"),
        SOURCE.replace("omitted or None", "omitted"),
    ],
)
def test_uncertain_or_unsupported_contract_has_no_patch(
    tmp_path: Path, source: str
) -> None:
    snap = manifest_snapshot(tmp_path, {"collect.py": source})
    module = analyze_snapshot(snap).modules[0]
    (item,) = maintenance_findings(snap, module)
    assert not maintenance_fix(snap, module, item, SHA).diff


def test_dictionary_keyword_only_default_preserves_encoding(tmp_path: Path) -> None:
    text = 'def add(é, *, entries={}):\r\n    """When entries is None or omitted, use a new dict on each call."""\r\n    entries[é] = 1\r\n    return entries'
    snap = manifest_snapshot(tmp_path, {"main.py": "\ufeff" + text})
    module = analyze_snapshot(snap).modules[0]
    (item,) = maintenance_findings(snap, module)
    patch = maintenance_fix(snap, module, item, SHA)
    assert "+\ufeffdef add(é, *, entries=None):\r\n" in patch.diff
    assert "+        entries = {}\r\n" in patch.diff
