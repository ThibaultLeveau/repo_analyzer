import shutil
from pathlib import Path

import pytest
from test_manifests import manifest_snapshot

from repopilot.artifacts import Artifacts
from repopilot.dependencies import analyze_dependencies
from repopilot.languages import analyze_snapshot
from repopilot.manifests import go_manifest, revision_declarations
from repopilot.module_graph import ModuleGraph
from repopilot.python_facts import Span

MOD = "example.org/store"
FILES = {
    "go.mod": f"module {MOD}\n\ngo 1.24\nrequire (\n example.net/lib v1.2.3\n example.net/lib/sub v0.2.0\n example.net/indirect v2.0.0 // indirect\n)\n",
    "cmd/main.go": f'package main\nimport (\n "fmt"\n ioalias "{MOD}/data"\n . "example.net/lib/sub/parser"\n _ "example.net/indirect"\n)\nfunc main() {{}}\n',
    "data/reader.go": "package data\ntype Reader[T any] struct { value T }\ntype Alias = Reader[int]\n"
    "func (r *Reader[T]) Read() T {\n return r.value\n}\n",
    "data/helper.go": "package data\nfunc Open() {}\n",
    "data/reader_test.go": "package data_test\nfunc testRead() {}\n",
    "bad.go": "package broken\nfunc (\n",
    "src/python.py": "def greet():\n    pass\n",
    "nested/go.mod": "module example.org/other\n",
    "nested/app.go": 'package other\nimport "example.org/store/data"\n',
}


def test_go_facts_without_toolchain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "")
    assert shutil.which("go") is None
    snap = manifest_snapshot(tmp_path, FILES)
    facts = analyze_snapshot(snap)
    assert [(d.path, d.line) for d in facts.diagnostics] == [("bad.go", 2)]
    modules = {m.span.path: m for m in facts.modules}
    pkg = modules["data/reader.go"]
    assert pkg.name == MOD + "/data" and pkg.name in facts.packages
    assert [(s.kind, s.span.start, s.span.end) for s in pkg.symbols] == [
        ("package", 1, 1),
        ("type", 2, 2),
        ("type", 3, 3),
        ("method", 4, 6),
    ]
    assert pkg.symbols[-1].name == MOD + "/data.Reader.Read"
    assert modules["src/python.py"].symbols[0].name == "python.greet"
    assert modules["data/reader_test.go"].name == MOD + "/data_test"
    imports = modules["cmd/main.go"].imports
    assert [(i.kind, i.alias, i.target, i.span.start) for i in imports] == [
        ("stdlib", None, "fmt", 3),
        ("local", "ioalias", MOD + "/data", 4),
        ("external", ".", "example.net/lib/sub", 5),
        ("unresolved", "_", None, 6),
    ]
    assert modules["nested/app.go"].imports[0].kind == "unresolved"
    assert {e.target.path for e in ModuleGraph(facts).edges} == {
        "data/reader.go",
        "data/helper.go",
    }
    deps = analyze_dependencies(snap).packages
    assert (
        deps["go:example.net/lib/sub"].files == 1
        and "go:example.net/indirect" not in deps
    )
    declared = deps["go:example.net/lib/sub"].declarations[0]
    assert declared.constraint == "v0.2.0" and declared.span == Span("go.mod", 6, 6)
    assert declared in revision_declarations("go.mod", FILES["go.mod"], "require")
    assert Artifacts().facts_version(snap) == "1-go1"


def test_manifest_and_single_file_without_module(tmp_path: Path) -> None:
    _, decls, diagnostics = go_manifest(
        "go.mod", 'module "unfinished\nrequire short\nrequire x.example/a v1.0.0\n'
    )
    assert [d.line for d in diagnostics] == [1, 2] and len(decls) == 1
    snap = manifest_snapshot(tmp_path, {"main.go": "package demo\nfunc Run() {}"})
    facts = analyze_snapshot(snap)
    assert not facts.diagnostics and facts.modules[0].symbols[-1].name == "demo.Run"
