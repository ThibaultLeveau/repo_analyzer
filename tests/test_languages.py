import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_cli import run_cli
from test_manifests import manifest_snapshot

from repopilot import languages
from repopilot.app import create_app
from repopilot.artifacts import Artifacts
from repopilot.dependencies import analyze_dependencies
from repopilot.languages import analyze
from repopilot.python_facts import (
    ImportFact,
    Module,
    Parser,
    PythonAnalyzer,
    Span,
    Symbol,
)
from repopilot.search import LexicalSearch
from repopilot.snapshots import SourceSnapshot


class ExampleAnalyzer:
    suffixes = (".demo",)

    def __init__(self) -> None:
        self.groups: list[tuple[str, ...]] = []

    def prepare(self, snapshot: SourceSnapshot, paths: tuple[str, ...]) -> Parser:
        self.groups.append(paths)

        def parse(path: str, text: str) -> Module:
            if text == "broken":
                raise ValueError("Invalid demo file")
            span = Span(path, 1, 1)
            name = path.removesuffix(".demo")
            return Module(
                name,
                False,
                span,
                None,
                (Symbol(name + ".draw", "function", span),),
                (ImportFact(name, "fixture", None, None, "fixture", "external", span),),
            )

        return Parser(parse)


def test_mixed_dispatch_keeps_failures_and_evidence_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = tmp_path / "executed"
    snap = manifest_snapshot(
        tmp_path / "sources",
        {
            "z.py": "def run():\n    pass\n",
            "bad.demo": "broken",
            "a.demo": "show widget",
            "README.txt": "unsupported searchable text",
            "analyzers/plugin.py": f"open({str(sentinel)!r}, 'w').close()",
        },
    )
    demo = ExampleAnalyzer()
    registry = (demo, PythonAnalyzer())
    facts = analyze(snap, registry)
    assert demo.groups == [("a.demo", "bad.demo")]
    assert [m.span.path for m in facts.modules] == [
        "a.demo",
        "analyzers/plugin.py",
        "z.py",
    ]
    assert [(d.path, d.message) for d in facts.diagnostics] == [
        ("bad.demo", "Invalid demo file")
    ]
    assert facts == analyze(snap, tuple(reversed(registry)))
    assert not analyze_dependencies(snap, facts=facts).packages
    with pytest.raises(ValueError, match="Multiple built-in"):
        analyze(snap, (demo, demo))
    monkeypatch.setattr(languages, "BUILTINS", registry)
    assert not sentinel.exists()
    assert any(hit.kind == "Symbol" for hit in LexicalSearch(snap).search("draw"))
    assert LexicalSearch(snap).search("unsupported searchable")[0].path == "README.txt"
    base = "/repos/owner/repo/" + "a" * 40
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        res = client.get(
            base + "/code",
            params={"path": "a.demo", "definition": "a.draw", "line": 1, "end": 1},
        )
        assert res.status_code == 200 and "a.draw" in res.text and 'id="L1"' in res.text
        assert "a.demo" in client.get(base + "/architecture").text
    assert not sentinel.exists()


def test_legacy_facts_reopen_without_parsing_and_export_through_installed_cli(
    tmp_path: Path, cli_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = Path(cli_env["REPOPILOT_DATA_DIR"])
    snap = manifest_snapshot(
        directory / "sources", {"src/old.py": "def hello():\n    pass\n"}
    )
    span = {"path": "src/old.py", "start": 1, "end": 2}
    legacy = {
        "modules": [
            {
                "name": "old",
                "is_package": False,
                "span": span,
                "docstring": None,
                "symbols": [
                    {
                        "name": "old.hello",
                        "kind": "function",
                        "span": span,
                        "is_async": False,
                    }
                ],
                "imports": [],
                "doc_span": None,
            }
        ],
        "packages": [],
        "diagnostics": [],
    }
    with closing(sqlite3.connect(snap.database.parent / "analysis.db")) as db, db:
        db.execute(
            "CREATE TABLE artifacts(kind TEXT,version TEXT,payload BLOB NOT NULL,PRIMARY KEY(kind,version))"
        )
        db.execute(
            "INSERT INTO artifacts VALUES('python','1',?)",
            (json.dumps(legacy).encode(),),
        )
    monkeypatch.setattr(
        "repopilot.artifacts.analyze_snapshot",
        lambda _: pytest.fail("Unexpected reanalysis"),
    )
    assert Artifacts().python(snap).modules[0].symbols[0].name == "old.hello"
    result = run_cli(
        cli_env | {"REPOPILOT_FIXTURE_OFFLINE": "1"},
        "export",
        "owner/repo",
        "--sha",
        "a" * 40,
    )
    assert result.returncode == 0, result.stderr
    roles = json.loads(result.stdout)["sections"]["Architecture"]
    assert roles[0]["title"] == "old" and roles[0]["evidence"][0]["url"].endswith(
        "/src/old.py#L1-L2"
    )
