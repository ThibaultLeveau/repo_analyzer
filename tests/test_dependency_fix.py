import subprocess
from pathlib import Path

import pytest
from test_manifests import manifest_snapshot

from repopilot.dependencies import analyze_dependencies
from repopilot.patches import dependency_fix


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        ("requirements.txt", "", "pyyaml\n"),
        (
            "requirements.txt",
            "# base\r\nrequests\r\n",
            "# base\r\nrequests\r\npyyaml\r\n",
        ),
        ("requirements.txt", "\ufeffrequests", "\ufeffrequests\npyyaml"),
        (
            "pyproject.toml",
            "[project]\ndependencies = []",
            '[project]\ndependencies = ["pyyaml"]',
        ),
        (
            "pyproject.toml",
            "[project]\ndependencies = ['requests']\n",
            "[project]\ndependencies = ['pyyaml', 'requests']\n",
        ),
        (
            "pyproject.toml",
            "[project]\r\ndependencies = [\r\n  # networking\r\n  'requests',\r\n]\r\n",
            "[project]\r\ndependencies = [\r\n  'pyyaml',\r\n  # networking\r\n  'requests',\r\n]\r\n",
        ),
    ],
)
def test_patch_applies_verbatim_and_reparses(
    tmp_path: Path, path: str, source: str, expected: str
) -> None:
    files = {path: source, "app.py": "from yaml import load\n"}
    snap = manifest_snapshot(tmp_path / "snapshot", files)
    report = analyze_dependencies(snap)
    patch = dependency_fix(snap, report, report.packages["pyyaml"])
    assert patch.path == path and patch.evidence and patch.evidence.start == 1
    assert snap.read_text(path) == source.removeprefix("\ufeff")
    work = tmp_path / "checkout"
    work.mkdir()
    manifest = work / path
    manifest.write_bytes(source.encode())
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "apply", "--whitespace=nowarn", "-"],
        cwd=work,
        input=patch.diff.encode(),
        check=True,
    )
    assert manifest.read_bytes() == expected.encode()
    updated = manifest_snapshot(tmp_path / "updated", files | {path: expected})
    result = analyze_dependencies(updated)
    assert result.packages["pyyaml"].declarations[0].constraint == ""
    assert not dependency_fix(updated, result, result.packages["pyyaml"]).diff


@pytest.mark.parametrize(
    ("files", "name"),
    [
        ({"requirements.txt": "", "app.py": "import cv2"}, "opencv-python"),
        ({"requirements.txt": "", "app.py": "import unknown"}, "unknown"),
        ({"requirements.txt": "", "tests/test_yaml.py": "import yaml"}, "pyyaml"),
        (
            {
                "requirements.txt": "",
                "pyproject.toml": "[project]",
                "app.py": "import yaml",
            },
            "pyyaml",
        ),
        (
            {
                "pyproject.toml": "[project]\ndynamic=['dependencies']",
                "app.py": "import yaml",
            },
            "pyyaml",
        ),
        (
            {"requirements.txt": "requests --hash=sha256:abc", "app.py": "import yaml"},
            "pyyaml",
        ),
    ],
)
def test_abstain_when_mapping_or_edit_is_ambiguous(
    tmp_path: Path, files: dict[str, str], name: str
) -> None:
    snapshot = manifest_snapshot(tmp_path, files)
    report = analyze_dependencies(snapshot)
    patch = dependency_fix(snapshot, report, report.packages[name])
    assert patch.reason and not patch.diff
