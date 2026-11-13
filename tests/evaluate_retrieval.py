import argparse
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from time import perf_counter

import httpx2
from journey_fixture import REPO, Catalog
from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from repopilot.models import RepositoryHead
from repopilot.python_facts import Span
from repopilot.rerank import MAX_CANDIDATES, Reranker
from repopilot.retrieval import HybridRetriever
from repopilot.search import CHUNK_CHARS, RESULT_LIMIT, LexicalSearch
from repopilot.snapshots import SnapshotCache
from repopilot.vectors import EmbeddingConfig, VectorIndex

CORPUS = Path(__file__).parent / "fixtures/retrieval-v1.json"


class Query(BaseModel):
    id: str
    question: str
    expected: list[Span]


class Corpus(BaseModel):
    version: int
    sha: str
    k: int
    queries: list[Query]
    minimum_recall: dict[str, dict[str, float]]


class CorpusVectors:
    terms = (
        ("safe_load", "deserialized"),
        ("pip install", "installation"),
        ("pyyaml",),
        ("test_config",),
        ("load_catalog",),
    )

    def embed(self, texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
        return [
            [
                float(any(term in text.casefold() for term in group))
                for group in self.terms
            ]
            for text in texts
        ]


@dataclass(frozen=True)
class Measurement:
    recall: float
    diversity: float
    elapsed_ms: float
    results: tuple[Span, ...]
    rerank_calls: int = 0


class FixtureRanker:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, messages: list[BaseMessage]) -> object:
        self.calls += 1
        raw = messages[-1].content
        assert isinstance(raw, str)
        payload = json.loads(raw)
        terms = set(re.findall(r"\w+", payload["question"].casefold()))
        ordered = sorted(
            payload["candidates"],
            key=lambda item: len(
                terms.intersection(re.findall(r"\w+", item["excerpt"].casefold()))
            ),
            reverse=True,
        )
        return {"ids": [item["id"] for item in ordered]}


def measure(
    expected: Sequence[Span], ranked: Sequence[Span], k: int, elapsed_ms: float = 0
) -> Measurement:
    if k <= 0 or not expected:
        raise ValueError("Provide positive k and at least one labeled range")
    results = tuple(ranked[:k])
    covered = sum(
        any(
            hit.path == ref.path and hit.start <= ref.end and ref.start <= hit.end
            for hit in results
        )
        for ref in set(expected)
    )
    diversity = len({hit.path for hit in results}) / len(results) if results else 0.0
    return Measurement(covered / len(set(expected)), diversity, elapsed_ms, results)


def evaluate(
    corpus: Corpus, directory: Path, *, compare_rerank: bool = False
) -> tuple[str, dict[str, dict[str, Measurement]]]:
    fixture = Catalog()
    if fixture.sha != corpus.sha:
        raise ValueError(
            "The evaluation fixture changed. Review the labeled source ranges."
        )
    cache = SnapshotCache(directory / "sources")
    with httpx2.Client(transport=httpx2.MockTransport(fixture.respond)) as client:
        snapshot = cache.fetch(RepositoryHead(REPO, "main", fixture.sha), client)
    lexical = LexicalSearch(snapshot)
    embedder = CorpusVectors()
    vectors = VectorIndex(
        directory, cache, embedder, EmbeddingConfig("fixture-v1", len(embedder.terms))
    )
    vectors.build(REPO, fixture.sha)
    hybrid = HybridRetriever(cache, vectors)
    hybrid.index(REPO, fixture.sha)

    def lexical_ranges(query: str) -> list[Span]:
        return [
            Span(h.path, h.start, h.end or h.start)
            for h in lexical.search(query)
            if h.start
        ]

    def vector_ranges(query: str) -> list[Span]:
        hits = vectors.nearest(REPO, fixture.sha, query, RESULT_LIMIT)
        hits.sort(
            key=lambda h: (
                h.distance,
                h.chunk.span.path,
                h.chunk.span.start,
                h.chunk.column,
            )
        )
        return [h.chunk.span for h in hits]

    def hybrid_ranges(query: str) -> list[Span]:
        return [s.span for s in hybrid.retrieve(REPO, fixture.sha, query).evidence()]

    modes: dict[str, Callable[[str], list[Span]]] = {
        "lexical": lexical_ranges,
        "vector": vector_ranges,
        "hybrid": hybrid_ranges,
    }
    fixture_ranker = FixtureRanker()
    if compare_rerank:
        reranker = Reranker(fixture_ranker)

        def reranked(query: str) -> list[Span]:
            sources = hybrid.retrieve(REPO, fixture.sha, query).evidence(
                limit=MAX_CANDIDATES, char_limit=MAX_CANDIDATES * CHUNK_CHARS
            )
            return [s.span for s in reranker.rank(query, sources)]

        modes["reranked"] = reranked
    measurements: dict[str, dict[str, Measurement]] = {}
    for mode, retrieve in modes.items():
        measurements[mode] = {}
        for query in corpus.queries:
            before = fixture_ranker.calls
            start = perf_counter()
            result = retrieve(query.question)
            elapsed = (perf_counter() - start) * 1000
            measurements[mode][query.id] = replace(
                measure(query.expected, result, corpus.k, elapsed),
                rerank_calls=fixture_ranker.calls - before,
            )
    return fixture.sha, measurements


def regressions(
    results: dict[str, dict[str, Measurement]], corpus: Corpus
) -> list[str]:
    return [
        f"{mode}/{query.id}"
        for mode, rows in results.items()
        if mode in corpus.minimum_recall
        for query in corpus.queries
        if rows[query.id].recall < corpus.minimum_recall[mode][query.id]
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval against the pinned Catalog fixture."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when a query falls below its checked-in recall baseline.",
    )
    parser.add_argument(
        "--compare-rerank",
        action="store_true",
        help="Compare hybrid retrieval with a deterministic fixture reranker.",
    )
    args = parser.parse_args()
    corpus = Corpus.model_validate_json(CORPUS.read_bytes())
    with TemporaryDirectory() as temporary:
        sha, results = evaluate(
            corpus, Path(temporary), compare_rerank=args.compare_rerank
        )
    failures = regressions(results, corpus) if args.check else []
    print(
        json.dumps(
            {
                "corpus_version": corpus.version,
                "repository": REPO,
                "sha": sha,
                "k": corpus.k,
                "modes": {
                    mode: {
                        "recall": mean(m.recall for m in rows.values()),
                        "diversity": mean(m.diversity for m in rows.values()),
                        "mean_elapsed_ms": mean(m.elapsed_ms for m in rows.values()),
                        "rerank_calls": sum(m.rerank_calls for m in rows.values()),
                        "queries": {key: asdict(row) for key, row in rows.items()},
                    }
                    for mode, rows in results.items()
                },
                "regressions": failures,
            },
            indent=2,
        )
    )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
