from pathlib import Path

from fastapi.testclient import TestClient
from test_manifests import manifest_snapshot

from repopilot.app import create_app
from repopilot.explorer import browse, highlight_python


def test_python_highlighting_preserves_source_and_multiline_state(
    tmp_path: Path,
) -> None:
    lines = [
        "# démarrage",
        "",
        's = """<b>',
        "λ\t&",
        '</b>"""',
        "if x < 2:",
        "    pass",
    ]
    snapshot = manifest_snapshot(
        tmp_path, {"example.py": "\r\n".join(lines), "plain.xyz": "\n".join(lines)}
    )
    source = browse(snapshot, "example.py", None, None)
    assert [
        "".join(text for _, text in parts) for parts in source.highlighting
    ] == lines
    assert source.highlighting[1] == []
    assert highlight_python(lines, 3, 4) == [[("string", "λ\t&")]]
    assert ("keyword", "if") in source.highlighting[5] and (
        "number",
        "2",
    ) in source.highlighting[5]
    assert browse(snapshot, "plain.xyz", None, None).highlighting == []
    incomplete = ["class Incomplete:", "    value = ("]
    result = highlight_python(incomplete, 0, 2)
    assert ["".join(text for _, text in parts) for parts in result] == incomplete


def test_selected_symbol_spanning_page_boundary(tmp_path: Path) -> None:
    snapshot = manifest_snapshot(
        tmp_path,
        {
            "example.py": "\n" * 198
            + "def begin():\n    x=1\n    return x\n"
            + "\n" * 250
        },
    )
    first = browse(snapshot, "example.py", 199, 201)
    assert first.first_line == 1 and first.lines[-1] == "    return x"
    following = browse(snapshot, "example.py", 199, 420, 2)
    assert following.first_line == 201 and len(following.lines) == 200


def test_tree_and_windowed_source(tmp_path: Path, explorer_url: str) -> None:
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        tree = client.get(explorer_url)
        assert tree.status_code == 200
        assert tree.text.index(">src</summary>") < tree.text.index(">A &amp; B.txt</a>")
        params = {"path": "src/demo.py", "line": "1", "end": "2"}
        source = client.get(explorer_url, params=params, headers={"HX-Request": "true"})
        assert "<!doctype" not in source.text and "<details open>" in source.text
        assert '"source-line selected"' in source.text
        assert "# original" in source.text and "&lt;tag&gt;" in source.text
        assert '<div id="L200"' in source.text and '<div id="L201"' not in source.text
        last = client.get(explorer_url, params=params | {"line": "201", "end": "201"})
        assert '<div id="L201" class="source-line selected">' in last.text
        assert "Previous lines" in last.text and "Next lines" not in last.text
        for invalid in (
            {"line": "0"},
            {"line": "202"},
            {"line": "2", "end": "1"},
            {"end": "2"},
        ):
            assert (
                client.get(
                    explorer_url, params={"path": "src/demo.py"} | invalid
                ).status_code
                == 422
            )
        newer = client.get(explorer_url.replace("a" * 40, "b" * 40), params=params)
        assert "# updated" in newer.text and "# original" not in newer.text
        assert "# original" in client.get(explorer_url, params=params).text


def test_unavailable_entries_and_missing_sources(
    tmp_path: Path, explorer_url: str
) -> None:
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        for name, notice in [
            ("A & B.txt", "This file is empty."),
            ("bin.dat", "Binary file."),
            ("large.log", "exceeds the text preview limit"),
            ("src", "Select a file in this directory"),
        ]:
            response = client.get(explorer_url, params={"path": name})
            assert response.status_code == 200 and notice in response.text
        for path in ("../README.md", "missing.py", "/absolute"):
            response = client.get(explorer_url, params={"path": path})
            assert response.status_code == 404 and "File not found" in response.text
        for revision in ("bad", "0" * 40):
            response = client.get(explorer_url.replace("a" * 40, revision))
            assert response.status_code == 404 and "Snapshot not found" in response.text
