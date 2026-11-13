from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_manifests import manifest_snapshot

from repopilot.app import create_app
from repopilot.search import (
    CHUNK_CHARS,
    CHUNK_LINES,
    QUERY_LIMIT,
    RESULT_LIMIT,
    LexicalSearch,
    chunks,
)

SHA = "a" * 40


def test_chunk_ranges_and_ids_are_bounded_and_commit_scoped(tmp_path: Path) -> None:
    long_line = "雪" * (CHUNK_CHARS * 2) + "last"
    lines = ["one"] * (CHUNK_LINES + 1) + [long_line, "end"]
    snapshot = manifest_snapshot(
        tmp_path, {"é.txt": "\r\n".join(lines), "empty.txt": "", "blank.txt": "  \n"}
    )
    indexed = list(chunks(snapshot, "owner/repo", SHA))
    assert indexed == list(chunks(snapshot, "owner/repo", SHA))
    ids = {chunk.id for chunk in indexed}
    assert len(ids) == len(indexed) == 6
    for repository, sha in (("owner/repo", "b" * 40), ("owner/other", SHA)):
        assert ids.isdisjoint(c.id for c in chunks(snapshot, repository, sha))
    assert ids == {c.id for c in chunks(snapshot, "Owner/Repo", SHA)}
    for c in indexed:
        assert (c.repository, c.sha, c.span.path) == ("owner/repo", SHA, "é.txt")
        assert len(c.text) <= CHUNK_CHARS and c.span.end - c.span.start < CHUNK_LINES
        excerpt = "\n".join(lines[c.span.start - 1 : c.span.end])
        assert excerpt[c.column : c.column + len(c.text)] == c.text
    assert [c.column for c in indexed if c.span.start == CHUNK_LINES + 2] == [
        0,
        CHUNK_CHARS,
        CHUNK_CHARS * 2,
    ]


def test_literal_search_ranks_symbols_and_returns_exact_lines(tmp_path: Path) -> None:
    definition = "def load_data():\n    return 3\n"
    long = "x" * (CHUNK_CHARS - 3) + "C++ [0] café"
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "a.py": definition,
            "b.py": definition,
            "refs/load_data.txt": "look up load_data",
            "notes.md": long + "\n" + "multiple\n" * (RESULT_LIMIT + 3),
            "empty.txt": "",
        },
    )
    search = LexicalSearch(snapshot)
    results = search.search("LOAD_DATA")
    assert [(r.kind, r.path, r.start, r.end) for r in results] == [
        ("Symbol", "a.py", 1, 2),
        ("Symbol", "b.py", 1, 2),
        ("File", "refs/load_data.txt", None, None),
        ("Source", "refs/load_data.txt", 1, 1),
    ]
    assert search.search("load_data", limit=2) == results[:2]
    assert search.search("empty.txt")[0].kind == "File"
    (literal,) = search.search("c++ [0] CAFÉ")
    assert literal.start == literal.end == 1 and literal.snippet.endswith(
        "C++ [0] café"
    )
    assert search.search("   ") == [] and search.search(".*") == []
    assert len(search.search("multiple", limit=500)) == RESULT_LIMIT
    with pytest.raises(ValueError, match="at most"):
        search.search("a" * (QUERY_LIMIT + 1))


def test_search_route_is_pinned_and_handles_overlong_queries(
    tmp_path: Path, explorer_url: str
) -> None:
    first = explorer_url.removesuffix("/code") + "/search"
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        assert "# original" in client.get(first, params={"q": "original"}).text
        assert (
            "No matches"
            in client.get(first.replace(SHA, "b" * 40), params={"q": "original"}).text
        )
        assert client.get(first.replace(SHA, "c" * 40)).status_code == 404
        assert (
            client.get(first, params={"q": "a" * (QUERY_LIMIT + 1)}).status_code == 422
        )
