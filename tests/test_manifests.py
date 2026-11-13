import io
import zipfile
from pathlib import Path

import httpx2

from repopilot.manifests import parse_manifests
from repopilot.models import RepositoryHead
from repopilot.snapshots import SnapshotCache, SourceSnapshot


def manifest_snapshot(
    root: Path,
    files: dict[str, str],
    *,
    repository: str = "owner/repo",
    sha: str = "a" * 40,
) -> SourceSnapshot:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        for path, source in files.items():
            archive.writestr("repo/" + path, source)
    with httpx2.Client(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, stream=httpx2.ByteStream(data.getvalue()))
        )
    ) as client:
        return SnapshotCache(root).fetch(
            RepositoryHead(repository, "main", sha), client
        )


def test_requirements_include_graph_is_local_bounded_and_cited(tmp_path: Path) -> None:
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "requirements.txt": "-r deps/shared.txt\nZed==2\n--requirement=https://invalid.example/list\n-r ../outside\n",
            "deps/shared.txt": '# note\nAcme_Client[X,y] >=1, <3 ; python_version > "3.10" # optional\n-r ../requirements.txt\n'
            "Tool @ https://example.org/tool.whl#sha256=abc\nThing==1 \\\n    --hash=sha256:123\n",
            "requirements/dev.in": "pytest\n--unsupported-option\n",
        },
    )
    report = parse_manifests(snapshot)
    assert report.paths == (
        "deps/shared.txt",
        "requirements.txt",
        "requirements/dev.in",
    )
    assert [(d.group, d.name) for d in report.declarations] == [
        ("dev", "pytest"),
        ("runtime", "acme-client"),
        ("runtime", "thing"),
        ("runtime", "tool"),
        ("runtime", "zed"),
    ]
    _, acme, thing, tool, zed = report.declarations
    assert (acme.constraint, acme.extras, acme.marker) == (
        ">=1, <3",
        ("x", "y"),
        'python_version > "3.10"',
    )
    assert acme.span.start == 2 and acme.source.endswith("# optional")
    assert (thing.span.start, thing.span.end, thing.constraint) == (5, 6, "==1")
    assert tool.url == "https://example.org/tool.whl#sha256=abc"
    assert zed.span.path == "requirements.txt" and zed.span.start == 2
    messages = [d.message for d in report.diagnostics]
    assert len(messages) == 4 and sum("local includes" in m for m in messages) == 2
    assert any("cycle" in m for m in messages)


def test_toml_tracks_exact_group_locations_even_with_duplicate_text(
    tmp_path: Path,
) -> None:
    contents = """[project]
description = "same==1"
dependencies = [
    "same==1", # runtime
]
dynamic = ["optional-dependencies"]
[project.optional-dependencies]
"A.B" = ["same==1"] # extra
[dependency-groups]
Test = ["Py_Test>=8"] # tests
dev = [{include-group="test"}, "Ruff"]
broken = [{include-group="missing"}]
"""
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "pyproject.toml": contents,
            "broken/pyproject.toml": "bad = [",
            "dotted/pyproject.toml": "project = {dependencies = ['opentelemetry_api']}\n",
        },
    )
    report = parse_manifests(snapshot)
    assert [(d.group, d.name, d.span.start) for d in report.declarations] == [
        ("group:dev", "py-test", 10),
        ("group:dev", "ruff", 11),
        ("group:test", "py-test", 10),
        ("optional:A.B", "same", 8),
        ("runtime", "opentelemetry-api", 1),
        ("runtime", "same", 4),
    ]
    assert "runtime" in report.declarations[-1].source
    for d in report.declarations:
        assert d.source == snapshot.lines(d.span.path)[d.span.start - 1]
    assert {d.path for d in report.diagnostics} == {
        "broken/pyproject.toml",
        "pyproject.toml",
    }
    assert any(d.line == 6 and "Dynamic" in d.message for d in report.diagnostics)
    assert any(d.line == 12 for d in report.diagnostics)


def test_dynamic_build_hooks_remain_data(tmp_path: Path) -> None:
    sentinel = tmp_path / "hook-ran"
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "pyproject.toml": '[build-system]\nbuild-backend="custom"\nbackend-path=["."]\n[project]\ndynamic=["dependencies"]',
            "custom.py": f"open({str(sentinel)!r}, 'w').close()",
        },
    )
    report = parse_manifests(snapshot)
    assert not report.declarations and report.diagnostics[0].line == 5
    assert not sentinel.exists()
