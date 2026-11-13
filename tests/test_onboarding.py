from pathlib import Path

from test_manifests import manifest_snapshot

from repopilot.onboarding import extract_setup


def test_documented_and_inferred_commands_keep_separate_evidence(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "ran"
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "README.md": (
                "# Demo\n## Prerequisites\n- Python 3.11 or later\n## Installation\n"
                "```sh\n$ python -m pip install legacy-package\n```\n"
                "### Windows\n```powershell\n.venv\\Scripts\\Activate.ps1\n```\n"
                "### Linux\n```bash\nsource .venv/bin/activate\n```\n"
                "## Usage\nUse `python legacy.py` to start.\n"
                "## Tests\n```sh\npytest \\\n  tests/unit\n```\n"
            ),
            "pyproject.toml": (
                '[project]\nname="current"\nrequires-python=">=3.12"\n'
                '[project.optional-dependencies]\ndev=["pytest"]\n'
                '[project.scripts]\ncurrent="demo:main"\n'
                '[tool.pytest.ini_options]\ntestpaths=["tests"]\n'
            ),
            "run.py": f"open({str(sentinel)!r}, 'w').close()",
            "compose.yaml": "services:\n  web:\n    image: example/web\n  db: {image: postgres}\n",
        },
    )
    report = extract_setup(snapshot)
    assert not report.empty and not sentinel.exists()
    assert [
        (s.category, s.text, s.span.start, s.span.end) for s in report.documented
    ] == [
        ("Prerequisite", "Python 3.11 or later", 3, 3),
        ("Install", "python -m pip install legacy-package", 6, 6),
        ("Configure", ".venv\\Scripts\\Activate.ps1", 10, 10),
        ("Configure", "source .venv/bin/activate", 14, 14),
        ("Run", "python legacy.py", 17, 17),
        ("Test", "pytest \\\n  tests/unit", 20, 21),
    ]
    assert [s.platform for s in report.documented[2:4]] == ["Windows", "POSIX"]
    assert [(s.text, s.span.path, s.span.start) for s in report.inferred] == [
        ("docker compose -f compose.yaml up web", "compose.yaml", 2),
        ("docker compose -f compose.yaml up db", "compose.yaml", 4),
        ("python -m pip install .", "pyproject.toml", 2),
        ("Python >=3.12", "pyproject.toml", 3),
        ('python -m pip install ".[dev]"', "pyproject.toml", 5),
        ("current", "pyproject.toml", 7),
        ("python -m pytest", "pyproject.toml", 9),
        ("python run.py", "run.py", 1),
    ]
    assert all(s.origin == "Documented" and not s.verified for s in report.documented)
    assert all(s.origin == "Inferred" and not s.verified for s in report.inferred)
    assert report == extract_setup(snapshot)


def test_inference_without_readme_and_insufficient_evidence(tmp_path: Path) -> None:
    fixture = manifest_snapshot(
        tmp_path / "one", {"requirements.txt": "flask\n", "pytest.ini": "[pytest]\n"}
    )
    report = extract_setup(fixture)
    assert not report.documented
    assert [s.text for s in report.inferred] == [
        "python -m pytest",
        "python -m pip install -r requirements.txt",
    ]
    empty = manifest_snapshot(
        tmp_path / "two",
        {
            "README.md": "# Project\nNo setup information.",
            "pyproject.toml": "[invalid",
            "compose.yml": "services: [",
            "other.py": "pass",
        },
    )
    assert extract_setup(empty).empty
