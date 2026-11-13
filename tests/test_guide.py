import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage
from markdown_it import MarkdownIt
from test_manifests import manifest_snapshot

from repopilot.app import create_app
from repopilot.architecture import Architecture
from repopilot.guide import Guides
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore

SHA = "a" * 40
PATH = f"/repos/owner/repo/{SHA}/getting-started"


@pytest.mark.parametrize(
    ("files", "documented", "inferred"),
    [
        ({"README.md": "## Usage\n`python old.py`"}, True, False),
        ({"run.py": 'print("hello")'}, False, True),
        (
            {"README.md": "## Usage\n`python old.py`", "run.py": 'print("hello")'},
            True,
            True,
        ),
        ({"other.txt": "hello"}, False, False),
    ],
)
def test_guide_keeps_setup_origins_and_commands(
    tmp_path: Path, files: dict[str, str], documented: bool, inferred: bool
) -> None:
    manifest_snapshot(tmp_path / "sources", files)
    guides = Guides(
        Architecture(
            SnapshotCache(tmp_path / "sources"), StateStore(tmp_path / "state.db")
        )
    )
    guide = guides.get("owner/repo", SHA)
    assert [s.title for s in guide.sections] == [
        "Installation",
        "Usage",
        "Architecture",
    ]
    groups = guide.sections[1].groups
    assert [i.text for i in groups["README instructions"]] == (
        ["python old.py"] if documented else []
    )
    assert [i.text for i in groups["Inferred steps"]] == (
        ["python run.py"] if inferred else []
    )
    markdown = guide.markdown()
    assert markdown.count("Unverified") == int(documented) + int(inferred)
    if documented or inferred:
        assert f"https://github.com/owner/repo/blob/{SHA}/" in markdown
        assert guide.sections[2].groups["First files to read"]
    else:
        assert guides.generate("owner/repo", SHA) == "insufficient_evidence"
        assert "No supporting evidence found." in markdown


class Notes:
    def __init__(self) -> None:
        self.valid = False
        self.calls = 0

    def invoke(self, messages: list[BaseMessage]) -> object:
        self.calls += 1
        payload = messages[-1].content
        assert isinstance(payload, str)
        source = json.loads(payload)["evidence"][0]
        key = source["id"] if self.valid else "unknown"
        return {
            "claims": [
                {"text": "Use the documented prerequisite.", "evidence_ids": [key]}
            ]
        }


def test_generated_notes_are_validated_and_persist_with_safe_download(
    tmp_path: Path,
) -> None:
    command = "python run.py --label '``` <b>value</b>'"
    manifest_snapshot(
        tmp_path / "sources",
        {
            "README.md": "## Prerequisites\nRequires Python <em>3</em> & recent pip.\n## Usage\n~~~~sh\n"
            + command
            + "\n~~~~\n"
        },
    )
    notes = Notes()
    with TestClient(
        create_app(data_dir=tmp_path, model=notes), base_url="http://localhost"
    ) as client:
        response = client.get(PATH)
        token = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
        assert token and "&lt;em&gt;" in response.text
        assert (
            client.post(PATH + "/notes", data={"csrf_token": "invalid"}).status_code
            == 403
        )
        form = {"csrf_token": token[1]}
        assert "No new cited notes" in client.post(PATH + "/notes", data=form).text
        assert "Use the documented prerequisite" not in client.get(PATH + ".md").text
        notes.valid = True
        assert (
            "Use the documented prerequisite."
            in client.post(PATH + "/notes", data=form).text
        )
        download = client.get(PATH + ".md")
        assert download.headers["content-disposition"].startswith("attachment")
        assert command in download.text and "Unverified" in download.text
        html = MarkdownIt("commonmark").render(download.text)
        assert "<em>" not in html and "<b>" not in html
        assert "&lt;em&gt;3&lt;/em&gt;" in html and "&lt;b&gt;value&lt;/b&gt;" in html
        missing = PATH.replace(SHA, "b" * 40)
        assert all(
            client.get(url).status_code == 404 for url in (missing, missing + ".md")
        )
    with TestClient(
        create_app(data_dir=tmp_path, model=notes), base_url="http://localhost"
    ) as reopened:
        assert reopened.get(PATH + ".md").text == download.text
        assert "Use the documented prerequisite." in html and notes.calls == 2
