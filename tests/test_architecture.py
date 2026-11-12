import io
import json
import re
import zipfile
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage

from repopilot.app import create_app
from repopilot.architecture import Architecture, PackageTree
from repopilot.models import RepositoryHead
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore


def test_generate_roles_requires_form_token_and_handles_no_key(
    tmp_path: Path, explorer_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    url = explorer_url.removesuffix("/code") + "/architecture"
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        page = client.get(url)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token
        data = {"path": "src/demo.py", "csrf_token": "invalid"}
        assert client.post(url + "/descriptions", data=data).status_code == 403
        response = client.post(
            url + "/descriptions", data=data | {"csrf_token": token[1]}
        )
        assert response.status_code == 200
        assert (
            "Static descriptions are available" in response.text
            and "Defines msg." in response.text
        )


class Descriptions:
    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    def invoke(self, messages: list[BaseMessage]) -> object:
        assert isinstance(messages[-1].content, str)
        sources = json.loads(messages[-1].content)["evidence"]
        self.calls.append({e["path"] for e in sources})
        if len(self.calls) > 1:
            raise TimeoutError
        return {
            "claims": [
                {"text": "Reads configuration.", "evidence_ids": [sources[0]["id"]]}
            ]
        }


def test_bounded_descriptions_leave_complete_inventory_after_failure(
    tmp_path: Path,
) -> None:
    cache = SnapshotCache(tmp_path / "sources")
    head = RepositoryHead("Owner/Demo", "main", "b" * 40)
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr(
            "demo/src/kit/__init__.py", '# header\n"""Read configuration."""'
        )
        for number in range(6):
            archive.writestr(
                f"demo/src/kit/module{number}.py", "def action():\n    pass"
            )
        archive.writestr("demo/tests/test_kit.py", "def test_read():\n    pass")
    transport = httpx2.MockTransport(
        lambda _: httpx2.Response(200, stream=httpx2.ByteStream(data.getvalue()))
    )
    with httpx2.Client(transport=transport) as client:
        cache.fetch(head, client)
    store = StateStore(tmp_path / "state.db")
    model = Descriptions()
    architecture = Architecture(cache, store, model)
    first, *_, last = architecture.roles(head.full_name, head.sha)
    assert first.module == "kit" and first.description == "Read configuration."
    assert last.module == "tests.test_kit" and last.description == "Defines test_read."
    assert not model.calls
    assert architecture.generate(head.full_name, head.sha, first.path) == "ok"
    assert architecture.generate(head.full_name, head.sha, last.path) == "timeout"
    assert [len(batch) for batch in model.calls] == [6, 2]
    reopened = Architecture(cache, StateStore(store.path))
    roles = reopened.roles(head.full_name, head.sha)
    assert len(roles) == 8 and sum(r.generated for r in roles) == 1
    assert roles[0].description == "Reads configuration."
    (evidence,) = reopened.evidence(head.full_name, head.sha, roles[0])
    assert evidence.span.start == evidence.span.end == 2
    assert evidence.excerpt == '"""Read configuration."""'
    assert f"{head.sha}/code?" in evidence.url
    tree = PackageTree.build(roles)
    assert set(tree.directories) == {"src", "tests"}
    assert len(tree.directories["src"].directories["kit"].files) == 7


def test_no_python_snapshot_has_an_empty_architecture(tmp_path: Path) -> None:
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        archive.writestr("demo/README.md", "Documentation only")
    head = RepositoryHead("Owner/Demo", "main", "c" * 40)
    with httpx2.Client(
        transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, stream=httpx2.ByteStream(data.getvalue()))
        )
    ) as client:
        SnapshotCache(tmp_path / "sources").fetch(head, client)
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as browser:
        response = browser.get(f"/repos/{head.full_name}/{head.sha}/architecture")
        assert (
            response.status_code == 200
            and "No readable Python modules" in response.text
        )
