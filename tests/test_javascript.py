import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_manifests import manifest_snapshot

from repopilot.artifacts import Artifacts
from repopilot.dependencies import analyze_dependencies
from repopilot.languages import analyze_snapshot
from repopilot.manifests import revision_declarations
from repopilot.module_graph import ModuleGraph
from repopilot.python_facts import Span
from repopilot.search import LexicalSearch

FILES = {
    "package.json": '{\n"dependencies": {\n  "@scope/client": "^2.3.1",\n  "requests": "~3"\n},\n"devDependencies": {"vitest":"4"}\n}',
    "requirements.txt": "requests==2\n",
    "python.py": "import requests\n",
    "src/ui.tsx": 'import Main, {load as fetchData} from "./core.js";\nimport {Client} from "@scope/client/http";\n'
    "export const Card = () => <section />;\nexport default class Panel {\n"
    '  async open() { return import("./core"); }\n}\n',
    "src/core.ts": "export default function start() {}\nexport function load() {\n  return 1;\n}\n",
    "src/exports.js": 'export {load as read} from "./core";\nimport "requests";\nimport "node:fs";\n'
    'import "@/alias";\nfunction lazy(name) { return import(name); }\n',
    "src/icon.jsx": "export default () => <svg />;\n",
    "src/bad.ts": "export const = ;",
    "src/one.ts": "export function ambiguous() {}",
    "src/one.js": "export function ambiguous() {}",
    "src/other.js": 'export * from "./one";',
}


def test_static_js_ts_imports_exports_and_cited_manifests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    assert shutil.which("node") is None
    snapshot = manifest_snapshot(tmp_path, FILES)
    facts = analyze_snapshot(snapshot)
    assert analyze_snapshot(snapshot) == facts
    assert [(d.path, d.line) for d in facts.diagnostics] == [("src/bad.ts", 1)]
    modules = {m.span.path: m for m in facts.modules}
    ui = modules["src/ui.tsx"]
    assert [(s.name, s.kind, s.span.start, s.span.end) for s in ui.symbols] == [
        ("src/ui.tsx.Card", "function", 3, 3),
        ("src/ui.tsx.Panel", "class", 4, 6),
        ("src/ui.tsx.Panel.open", "method", 5, 5),
    ]
    assert ui.symbols[-1].is_async and modules["src/icon.jsx"].symbols[0].name.endswith(
        ".default"
    )
    assert [(i.name, i.alias, i.target) for i in ui.imports[:2]] == [
        ("default", "Main", "src/core.ts"),
        ("load", "fetchData", "src/core.ts"),
    ]
    assert ui.imports[-1].dynamic and ui.imports[-1].target == "src/core.ts"
    assert [(i.kind, i.name, i.alias) for i in modules["src/exports.js"].imports] == [
        ("local", "load", "read"),
        ("external", None, None),
        ("stdlib", None, None),
        ("unresolved", None, None),
        ("unresolved", None, None),
    ]
    assert modules["src/other.js"].imports[0].kind == "unresolved"
    assert any(
        edge.target.path == "src/core.ts"
        and edge.source.path == "src/ui.tsx"
        and edge.sites == (Span("src/ui.tsx", 1, 1),)
        for edge in ModuleGraph(facts).edges
    )
    deps = analyze_dependencies(snapshot)
    pkg = deps.packages["npm:@scope/client"]
    assert pkg.files == 1 and pkg.declarations[0].constraint == "^2.3.1"
    assert pkg.declarations[0].span == Span("package.json", 3, 3)
    assert pkg.uses[0].fact.span == Span("src/ui.tsx", 2, 2)
    assert deps.packages["npm:requests"].uses[0].fact.span.path == "src/exports.js"
    assert deps.packages["requests"].uses[0].fact.span.path == "python.py"
    assert pkg.declarations[0] in revision_declarations(
        "package.json", FILES["package.json"], "dependencies"
    )
    assert LexicalSearch(snapshot).search("Panel.open")[0].path == "src/ui.tsx"


def test_older_mixed_cache_is_rebuilt_and_bad_manifest_is_diagnosed(
    tmp_path: Path,
) -> None:
    snapshot = manifest_snapshot(
        tmp_path, FILES | {"sub/package.json": "{", "sub/demo.ts": "import 'requests'"}
    )
    with closing(sqlite3.connect(snapshot.database.parent / "analysis.db")) as db, db:
        db.execute(
            "CREATE TABLE artifacts(kind TEXT,version TEXT,payload BLOB,PRIMARY KEY(kind,version))"
        )
        db.execute(
            "INSERT INTO artifacts VALUES('python','1',?)",
            (b'{"modules":[],"packages":[],"diagnostics":[]}',),
        )
    artifacts = Artifacts()
    assert analyze_snapshot(snapshot).modules[-1].span.path == "sub/demo.ts"
    assert analyze_snapshot(snapshot).modules[-1].imports[0].kind == "unresolved"
    assert any(m.span.path == "src/core.ts" for m in artifacts.python(snapshot).modules)
    assert any(
        d.path == "sub/package.json"
        for d in artifacts.dependencies(snapshot).manifests.diagnostics
    )
    assert artifacts.saved_analysis(snapshot)[:2] == (
        artifacts.python(snapshot),
        artifacts.dependencies(snapshot),
    )
