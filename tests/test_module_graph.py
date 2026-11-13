from dataclasses import replace
from pathlib import Path

from test_manifests import manifest_snapshot

from repopilot.module_graph import NEIGHBORS_PER_PAGE, ModuleGraph, arrow
from repopilot.python_facts import analyze_python


def test_import_graph_preserves_cycles_sites_and_isolated_modules(
    tmp_path: Path,
) -> None:
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "from . import b\nimport pkg.b as helper\nimport pkg.unknown\nimport requests",
            "pkg/b.py": "from .a import run\nfrom . import b\n",
            "empty.py": "",
        },
    )
    facts = analyze_python(snapshot)
    graph = ModuleGraph(facts).neighborhood("pkg/a.py")
    assert [n.name for n in graph.nodes] == ["empty", "pkg", "pkg.a", "pkg.b"]
    assert [
        (e.source.name, e.target.name, [s.start for s in e.sites]) for e in graph.edges
    ] == [("pkg.a", "pkg.b", [1, 2]), ("pkg.b", "pkg.a", [1]), ("pkg.b", "pkg.b", [2])]
    reordered = replace(facts, modules=tuple(reversed(facts.modules)))
    assert ModuleGraph(reordered).neighborhood("pkg/a.py") == graph
    a, b = (graph.positions()[n] for n in graph.nodes[-2:])
    assert len({arrow(a, b), arrow(b, a), arrow(b, b)}) == 3


def test_selected_neighborhood_is_bounded_and_every_neighbor_is_reachable(
    tmp_path: Path,
) -> None:
    names = [f"n{i:02d}" for i in range(NEIGHBORS_PER_PAGE + 2)]
    files = {f"{name}.py": "" for name in names}
    files["hub.py"] = "\n".join(f"import {name}" for name in reversed(names))
    files["isolated.py"] = "pass"
    graph = ModuleGraph(analyze_python(manifest_snapshot(tmp_path, files)))
    first, last = (graph.neighborhood("hub.py", page) for page in (0, 999))
    assert first.page == 1 and last.page == last.pages == 2
    assert len(first.nodes) == NEIGHBORS_PER_PAGE + 1
    assert [edge.target.name for edge in (*first.edges, *last.edges)] == names
    isolated = graph.neighborhood("isolated.py")
    assert len(isolated.nodes) == 1 and not isolated.edges


def test_ambiguous_module_and_unreadable_target_have_no_edges(tmp_path: Path) -> None:
    files = {
        "src/a.py": "",
        "a.py": "",
        "broken.py": "!invalid",
        "main.py": "import a, broken",
    }
    graph = ModuleGraph(analyze_python(manifest_snapshot(tmp_path, files)))
    assert sorted(graph.nodes) == ["a.py", "main.py", "src/a.py"]
    assert not graph.edges
