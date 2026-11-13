import hashlib
import json
import math
import os
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import batched
from pathlib import Path
from typing import Protocol, cast

import chromadb
from chromadb.api.client import Client
from chromadb.api.models.Collection import Collection
from chromadb.config import Settings
from chromadb.errors import NotFoundError
from filelock import FileLock, Timeout
from openai import OpenAI, OpenAIError

from repopilot.artifacts import CHUNK_SCHEMA
from repopilot.python_facts import Span
from repopilot.search import RESULT_LIMIT, Chunk, chunks
from repopilot.snapshots import SnapshotCache

BATCH_SIZE = 32


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str = "text-embedding-3-small"
    dimensions: int = 512

    def __post_init__(self) -> None:
        if not self.model.strip() or not 1 <= self.dimensions <= 3072:
            raise ValueError("Invalid embedding configuration")


class VectorError(RuntimeError):
    pass


class Embedder(Protocol):
    def embed(self, texts: list[str], config: EmbeddingConfig) -> list[list[float]]: ...


class OpenAIEmbedder:
    def embed(self, texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise VectorError("Set OPENAI_API_KEY to use semantic search.")
        try:
            with OpenAI(
                api_key=key,
                base_url="https://api.openai.com/v1",
                timeout=30,
                max_retries=1,
            ) as client:
                reply = client.embeddings.create(
                    input=texts,
                    model=config.model,
                    dimensions=config.dimensions,
                    encoding_format="float",
                )
        except OpenAIError as exc:
            raise VectorError(
                "The embedding provider is unavailable. Retry to resume."
            ) from exc
        records = sorted(reply.data, key=lambda item: item.index)
        if [r.index for r in records] != list(range(len(texts))):
            raise VectorError("The embedding provider returned an incomplete batch.")
        return [r.embedding for r in records]


@dataclass(frozen=True)
class Neighbor:
    chunk: Chunk
    distance: float


def _persistent_client(path: Path) -> Client:
    return cast(
        Client,
        chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False)
        ),
    )


def initialize_chroma(directory: Path) -> None:
    _persistent_client(directory / "chroma").close()


class VectorIndex:
    def __init__(
        self,
        directory: Path,
        cache: SnapshotCache,
        embedder: Embedder | None = None,
        config: EmbeddingConfig = EmbeddingConfig(),
    ) -> None:
        self.directory = directory / "chroma"
        self.cache = cache
        self.embedder = embedder or OpenAIEmbedder()
        self.config = config

    def _name(self, repository: str, sha: str) -> str:
        key = [
            CHUNK_SCHEMA,
            repository.lower(),
            sha,
            self.config.model,
            self.config.dimensions,
        ]
        return "snapshot-" + hashlib.sha256(json.dumps(key).encode()).hexdigest()

    def _client(self) -> Client:
        return _persistent_client(self.directory)

    def _embed(self, texts: list[str]) -> list[Sequence[float] | Sequence[int]]:
        vectors = self.embedder.embed(texts, self.config)
        if len(vectors) != len(texts) or any(
            len(v) != self.config.dimensions or not all(map(math.isfinite, v))
            for v in vectors
        ):
            raise VectorError("The embedding provider returned invalid vectors.")
        return list(vectors)

    def _add_missing(self, collection: Collection, batch: tuple[Chunk, ...]) -> None:
        items = {c.id: c for c in batch}
        existing = collection.get(ids=list(items), include=[])["ids"]
        for key in existing:
            del items[key]
        if not items:
            return
        documents = [c.text for c in items.values()]
        collection.upsert(
            ids=list(items),
            documents=documents,
            embeddings=self._embed(documents),
            metadatas=[
                {
                    "path": c.span.path,
                    "start": c.span.start,
                    "end": c.span.end,
                    "column": c.column,
                }
                for c in items.values()
            ],
        )

    def build(self, repository: str, sha: str) -> int:
        snapshot = self.cache.open(repository, sha)
        name = self._name(repository, sha)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            with (
                FileLock(self.directory / f"{name}.lock", timeout=0),
                self._client() as client,
            ):
                collection = client.get_or_create_collection(
                    name, embedding_function=None
                )
                if (collection.metadata or {}).get("ready"):
                    return collection.count()
                for batch in batched(chunks(snapshot, repository, sha), BATCH_SIZE):
                    self._add_missing(collection, batch)
                collection.modify(metadata={"ready": True})
                return collection.count()
        except Timeout as exc:
            raise VectorError(
                "Indexing is already in progress for this snapshot."
            ) from exc

    def nearest(
        self, repository: str, sha: str, query: str, limit: int = 5
    ) -> list[Neighbor]:
        if not query.strip() or limit < 1 or not self.directory.is_dir():
            return []
        with self._client() as client:
            try:
                collection = client.get_collection(
                    self._name(repository, sha), embedding_function=None
                )
            except NotFoundError:
                return []
            if not (collection.metadata or {}).get("ready") or not (
                count := collection.count()
            ):
                return []
            response = collection.query(
                query_embeddings=self._embed([query]),
                n_results=min(limit, count, RESULT_LIMIT),
                include=["documents", "metadatas", "distances"],
            )
        documents, metadata, distances = (
            response["documents"],
            response["metadatas"],
            response["distances"],
        )
        if documents is None or metadata is None or distances is None:
            raise VectorError("The local index returned an incomplete result.")
        result = []
        for text, data, distance in zip(
            documents[0], metadata[0], distances[0], strict=True
        ):
            path, start, end, column = (
                data.get("path"),
                data.get("start"),
                data.get("end"),
                data.get("column"),
            )
            if (
                not isinstance(path, str)
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not isinstance(column, int)
            ):
                raise VectorError("The local index returned an invalid source range.")
            result.append(
                Neighbor(
                    Chunk(repository, sha, Span(path, start, end), column, text),
                    distance,
                )
            )
        return result
