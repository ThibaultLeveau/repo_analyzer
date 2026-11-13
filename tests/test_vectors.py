import re
from pathlib import Path

import pytest
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
from fastapi.testclient import TestClient
from test_manifests import manifest_snapshot

from repopilot import vectors
from repopilot.app import create_app
from repopilot.search import chunks
from repopilot.snapshots import SnapshotCache
from repopilot.vectors import EmbeddingConfig, VectorError, VectorIndex

REPO = "owner/repo"
SHA = "a" * 40


class Vectors:
    def __init__(self) -> None:
        self.batches: list[list[str]] = []
        self.interrupt = False

    def embed(self, texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
        self.batches.append(texts)
        if self.interrupt and len(self.batches) == 2:
            raise VectorError("Provider quota reached.")
        return [
            ([1.0, 0.0] if "database" in text else [0.0, float(len(text))])
            + [0.0] * (config.dimensions - 2)
            for text in texts
        ]


def test_reopen_resume_and_query_only_a_complete_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_download(*args: object, **kwargs: object) -> None:
        pytest.fail("The Chroma default embedding function ran")

    monkeypatch.setattr(DefaultEmbeddingFunction, "__call__", unexpected_download)
    monkeypatch.setattr(vectors, "BATCH_SIZE", 2)
    files = {
        "src/build.txt": "Build the package.",
        "src/format.txt": "Format the source tree.",
        "src/lint.txt": "Lint sources.",
        "src/target.txt": "\nUse the database for persistence.\n",
    }
    config = EmbeddingConfig("fixture", 2)
    fixture = Vectors()
    embeddings = dict(
        zip(files, fixture.embed(list(files.values()), config), strict=True)
    )
    assert len(set(map(tuple, embeddings.values()))) == len(files)
    assert fixture.embed(["database"], config) == [embeddings["src/target.txt"]]

    snapshot = manifest_snapshot(tmp_path / "sources", files)
    cache = SnapshotCache(tmp_path / "sources")
    provider = Vectors()
    provider.interrupt = True
    index = VectorIndex(tmp_path, cache, provider, config)
    assert index.nearest(REPO, SHA, "database") == [] and not provider.batches
    with pytest.raises(VectorError, match="quota"):
        index.build(REPO, SHA)
    assert [len(b) for b in provider.batches] == [2, 2]
    assert any("database" in text for text in provider.batches[1])
    assert index.nearest(REPO, SHA, "database") == [] and len(provider.batches) == 2

    resumed = VectorIndex(tmp_path, cache, provider, config)
    assert resumed.build(REPO, SHA) == len(files)
    assert provider.batches[-1] == provider.batches[-2]
    assert resumed.build(REPO.upper(), SHA) == len(files) and len(provider.batches) == 3
    other_config = VectorIndex(tmp_path, cache, provider, EmbeddingConfig("fixture", 3))
    assert other_config.nearest(REPO, SHA, "database") == []
    assert resumed.nearest("owner/other", SHA, "database") == []
    assert (
        resumed.nearest(REPO, "b" * 40, "database") == [] and len(provider.batches) == 3
    )
    (result,) = resumed.nearest(REPO, SHA, "database", limit=1)
    expected = next(
        c for c in chunks(snapshot, REPO, SHA) if c.span.path.endswith("target.txt")
    )
    assert result.chunk == expected and result.distance == pytest.approx(0)
    assert provider.batches[-1] == ["database"]


def test_key_failure_leaves_the_explorer_and_lexical_search_available(
    tmp_path: Path, explorer_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    url = explorer_url.removesuffix("/code") + "/search"
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        page = client.get(url)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
        assert token
        assert (
            client.post(url + "/index", data={"csrf_token": "invalid"}).status_code
            == 403
        )
        assert (
            "Set OPENAI_API_KEY"
            in client.post(url + "/index", data={"csrf_token": token[1]}).text
        )
        assert client.get(explorer_url).status_code == 200
        assert "demo.msg" in client.get(url, params={"q": "msg"}).text


def test_empty_and_invalid_embedding_results(tmp_path: Path) -> None:
    class Invalid:
        def embed(self, texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
            return [[float("nan")]]

    cache = SnapshotCache(tmp_path / "sources")
    manifest_snapshot(cache.root, {"source.txt": "some text"})
    index = VectorIndex(tmp_path, cache, Invalid(), EmbeddingConfig("fixture", 1))
    with pytest.raises(VectorError, match="invalid vectors"):
        index.build(REPO, SHA)
    assert index.nearest(REPO, SHA, "query") == []
    empty_cache = SnapshotCache(tmp_path / "empty-sources")
    manifest_snapshot(empty_cache.root, {"source.txt": ""})
    empty = VectorIndex(tmp_path / "empty", empty_cache, Invalid())
    assert empty.build(REPO, SHA) == 0 and empty.nearest(REPO, SHA, "query") == []
