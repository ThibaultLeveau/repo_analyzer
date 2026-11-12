import io
import zipfile
from pathlib import Path

import httpx2

from repopilot.models import RepositoryHead
from repopilot.python_facts import Span, analyze_python
from repopilot.snapshots import SnapshotCache


def test_python_facts_keep_scope_lines_and_provenance(tmp_path: Path) -> None:
    sentinel = tmp_path / "executed"
    files = {
        "src/kit/__init__.py": '"""An example package."""\nfrom . import core\n',
        "src/kit/core.py": (
            '"Résumé"\n'
            "import pathlib as paths, requests\n"
            "from . import sibling as other\n"
            "from ..absent import thing\n"
            "label: str = '日本語'\n"
            "@decorator\n"
            "class Reader:\n"
            "    @staticmethod\n"
            "    async def load(input):\n"
            "        def clean():\n"
            "            return input\n"
            "        first, *rest = clean()\n"
            "        return first\n"
            f"open({str(sentinel)!r}, 'w').close()\n"
        ),
        "src/kit/sibling.py": "",
        "src/kit/namespace/child.py": "from ..core import Reader\n",
        "src/kit/broken.py": "pass\nif (:\n",
        "docs/example.txt": "ignored",
    }
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for path, source in reversed(files.items()):
            archive.writestr("repo/" + path, source)
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, stream=httpx2.ByteStream(data.getvalue()))
    )
    with httpx2.Client(transport=transport) as client:
        snapshot = SnapshotCache(tmp_path).fetch(
            RepositoryHead("a/b", "trunk", "1" * 40), client
        )

    facts = analyze_python(snapshot)
    assert analyze_python(snapshot) == facts
    assert not sentinel.exists()
    assert facts.packages == ("kit", "kit.namespace")
    package, core, child, sibling = facts.modules
    assert package.is_package and package.docstring == "An example package."
    assert sibling.name == "kit.sibling" and not sibling.symbols
    assert core.docstring == "Résumé" and core.span == Span("src/kit/core.py", 1, 14)
    assert [(s.name, s.kind, s.span.start, s.span.end) for s in core.symbols] == [
        ("kit.core.label", "assignment", 5, 5),
        ("kit.core.Reader", "class", 6, 13),
        ("kit.core.Reader.load", "method", 8, 13),
        ("kit.core.Reader.load.clean", "function", 10, 11),
        ("kit.core.Reader.load.first", "assignment", 12, 12),
        ("kit.core.Reader.load.rest", "assignment", 12, 12),
    ]
    assert core.symbols[2].is_async and not core.symbols[3].is_async
    assert [(i.target, i.kind, i.alias) for i in core.imports] == [
        ("pathlib", "stdlib", "paths"),
        ("requests", "external", None),
        ("kit.sibling", "local", "other"),
        (None, "unresolved", None),
    ]
    assert core.imports[2].span == Span("src/kit/core.py", 3, 3)
    assert (
        child.imports[0].module,
        child.imports[0].name,
        child.imports[0].target,
    ) == ("..core", "Reader", "kit.core")
    assert [(d.path, d.line) for d in facts.diagnostics] == [("src/kit/broken.py", 2)]
