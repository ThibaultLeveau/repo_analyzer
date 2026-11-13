from pathlib import Path

from test_manifests import manifest_snapshot

from repopilot.python_facts import Symbol, analyze_python
from repopilot.references import ReferenceIndex

FILES = {
    "src/core.py": "def load(value):\n    return load(value - 1) if value else 0\n\nclass Row:\n    def read(self):\n        return 0\n",
    "src/other.py": "class Row:\n    def read(self):\n        return 1\n",
    "src/client.py": "import core as c\nfrom other import Row as Other\nfrom core import load as parse\n\ndef run(obj):\n    parse(1)\n    c.Row.read(obj)\n    Other.read(obj)\n    return obj.read()\n\ndef indirect(parse):\n    return parse(2)\n",
    "tests/test_loader.py": "from core import load\n\ndef test_load():\n    assert load(2) == 0\n",
}


def test_reference_index_resolves_aliases_recursion_and_test_calls(
    tmp_path: Path,
) -> None:
    snapshot = manifest_snapshot(tmp_path, FILES)
    index = ReferenceIndex(snapshot, analyze_python(snapshot))
    detail = index.detail(index.definitions["core.load"])
    assert [(r.kind, r.caller, r.span.start) for r in detail.incoming] == [
        ("import", "client", 3),
        ("call", "client.run", 6),
        ("call", "core.load", 2),
    ]
    assert [(r.kind, r.span.start) for r in detail.tests] == [
        ("import", 1),
        ("call", 4),
    ]
    assert len(detail.outgoing) == 1 and detail.outgoing[0].target == detail.symbol
    caller = index.detail(index.definitions["client.run"])
    assert [(r.text, r.target.name if r.target else None) for r in caller.outgoing] == [
        ("parse", "core.load"),
        ("c.Row.read", "core.Row.read"),
        ("Other.read", "other.Row.read"),
        ("obj.read", None),
    ]
    (shadowed,) = index.detail(index.definitions["client.indirect"]).outgoing
    assert shadowed.target is None
    sources = detail.evidence(snapshot, "owner/repo", "a" * 40)
    assert all(s.excerpt and s.sha == "a" * 40 for s in sources)
    assert sources[0].span == detail.symbol.span


def test_rebound_names_and_dynamic_calls_have_no_target(tmp_path: Path) -> None:
    files = FILES | {
        "src/shadow.py": "from core import load as alias\nfrom other import Row as alias\n"
        "def run(value):\n    alias(value)\n    getattr(value, 'read')()\n"
        "    [alias(1) for alias in value]\n"
    }
    snapshot = manifest_snapshot(tmp_path, files)
    index = ReferenceIndex(snapshot, analyze_python(snapshot))
    refs = index.detail(index.definitions["shadow.run"]).outgoing
    assert refs and all(r.target is None for r in refs)
    module_import = next(
        r for r in index.refs if r.kind == "import" and r.text == "core"
    )
    assert module_import.target and not isinstance(module_import.target, Symbol)
    assert module_import.target.span.path == "src/core.py"


def test_definition_expressions_run_in_the_enclosing_scope(tmp_path: Path) -> None:
    source = (
        "def factory(): return object\n"
        "def build():\n"
        "    @factory()\n"
        "    def inner(value=factory()): pass\n"
        "    class Kind(factory()): pass\n"
    )
    snapshot = manifest_snapshot(tmp_path, {"sample.py": source})
    index = ReferenceIndex(snapshot, analyze_python(snapshot))
    calls = index.detail(index.definitions["sample.build"]).outgoing
    assert [r.span.start for r in calls] == [3, 4, 5]
    assert all(r.target == index.definitions["sample.factory"] for r in calls)
