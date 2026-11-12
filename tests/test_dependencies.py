from pathlib import Path

from fastapi.testclient import TestClient
from test_manifests import manifest_snapshot

from repopilot.app import create_app
from repopilot.dependencies import analyze_dependencies


def test_match_import_evidence_conservatively(tmp_path: Path) -> None:
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "pyproject.toml": '[project]\ndependencies=["Pillow", "yaml", "opencv-python", "opencv-python-headless", "unused"]\n'
            '[project.optional-dependencies]\ntest=["pytest"]',
            "src/yaml.py": "pass",
            "src/core.py": "import yaml, sys\nfrom PIL import Image\nfrom PIL import ImageFile\nimport cv2\n"
            "import importlib\nloaded = importlib.import_module('camera_plugin')\n",
            "tests/test_core.py": "import pytest\nfrom PIL import Image\n",
        },
    )
    packages = analyze_dependencies(snapshot).packages
    assert "sys" not in packages and "importlib" not in packages
    assert packages["pillow"].files == 2 and len(packages["pillow"].uses) == 3
    assert packages["pillow"].uses[0].fact.span.start == 2
    assert not packages["yaml"].uses and not packages["unused"].uses
    assert packages["unused"].observation == "No static usage found"
    assert packages["pytest"].observation == "Used only in tests"
    assert packages["pytest"].declarations[0].group == "optional:test"
    assert packages["opencv-python"].observation == "Ambiguous distribution"
    assert packages["opencv-python"].files == 0
    possible = packages["opencv-python-headless"].uses[0]
    assert set(possible.candidates) == {"opencv-python", "opencv-python-headless"}
    missing = packages["camera-plugin"]
    assert missing.observation == "Possibly undeclared" and missing.uses[0].fact.dynamic
    assert missing.uses[0].fact.span.start == 6


def test_dependency_page_without_manifests(tmp_path: Path) -> None:
    manifest_snapshot(tmp_path / "sources", {"test.py": "import json"})
    url = "/repos/owner/repo/" + "a" * 40 + "/dependencies"
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        response = client.get(url)
        assert (
            response.status_code == 200
            and "No dependency manifests found" in response.text
        )
        assert "No third-party packages identified" in response.text
        assert client.get(url, params={"package": "missing"}).status_code == 404
