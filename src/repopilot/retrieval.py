import heapq
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, replace
from functools import lru_cache

from repopilot.events import measure
from repopilot.grounding import Evidence
from repopilot.search import (
    CHUNK_CHARS,
    QUERY_LIMIT,
    RESULT_LIMIT,
    Chunk,
    Hit,
    LexicalSearch,
    chunks,
)
from repopilot.snapshots import SnapshotCache
from repopilot.vectors import Neighbor, VectorError, VectorIndex

CANDIDATE_LIMIT = 24
SOURCE_LIMIT = 6
CONTEXT_CHARS = 6000
_STOP = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "can", "code", "defined", "do", "does", "file", "for", "from", "how", "i", "in", "is", "it", "me", "of", "on", "or", "project", "repository", "show", "that", "the", "this", "to", "us", "use", "what", "where", "which", "who", "why", "with"]
)


def _exact(hit: Hit, term: str) -> bool:
    label = hit.label.casefold()
    if hit.kind == "File":
        return term in {label, label.rsplit("/", 1)[-1]}
    if hit.kind == "Symbol":
        return term in {label, label.rsplit(".", 1)[-1]}
    return False


@dataclass(frozen=True)
class Match:
    chunk: Chunk
    hit: Hit
    score: float


@dataclass(frozen=True)
class Retrieval:
    matches: list[Match]
    literal_hits: list[Hit]
    term: str

    def evidence(self) -> list[Evidence]:
        sources: dict[str, Evidence] = {}
        remaining = CONTEXT_CHARS
        for match in self.matches:
            chunk = match.chunk
            if len(chunk.text) > remaining:
                continue
            source = Evidence(chunk.repository, chunk.sha, chunk.span, chunk.text)
            if source.id not in sources:
                sources[source.id] = source
                remaining -= len(chunk.text)
            if len(sources) == SOURCE_LIMIT:
                break
        return list(sources.values())

    def hits(self) -> list[Hit]:
        if not any("Semantic" in m.hit.channel for m in self.matches):
            return self.literal_hits
        ranked = [m.hit for m in self.matches]
        represented = {(h.path, h.kind, h.start, h.label) for h in ranked}
        others = [
            h
            for h in self.literal_hits
            if (h.path, h.kind, h.start, h.label) not in represented
        ]
        combined = ranked + others
        combined.sort(key=lambda hit: not _exact(hit, self.term))
        return combined[:RESULT_LIMIT]


class HybridRetriever:
    def __init__(self, cache: SnapshotCache, vectors: VectorIndex) -> None:
        self.cache = cache
        self.vectors = vectors
        self.index = lru_cache(maxsize=2)(self._index)

    def _index(self, repo: str, sha: str) -> LexicalSearch:
        return LexicalSearch(self.cache.open(repo, sha))

    @measure("retrieval.search")
    def retrieve(
        self, repo: str, sha: str, query: str, selected: str = ""
    ) -> Retrieval:
        if not query.strip():
            return Retrieval([], [], "")
        index = self.index(repo, sha)
        literals = index.search(query) if len(query) <= QUERY_LIMIT else []
        by_path: dict[str, list[tuple[int, Hit]]] = defaultdict(list)
        for i, hit in enumerate(literals):
            by_path[hit.path].append((i, hit))
        try:
            neighbors = self.vectors.nearest(repo, sha, query, CANDIDATE_LIMIT)
        except VectorError:
            neighbors = []
        semantic = {n.chunk.id: n for n in neighbors[:CANDIDATE_LIMIT]}
        valid: list[Neighbor] = []
        words = set(re.findall(r"\w+", query.casefold())) - _STOP
        labels: dict[str, Hit] = {}

        def candidates() -> Iterator[tuple[int, Chunk, Hit | None]]:
            for chunk in chunks(index.snapshot, repo, sha):
                key = chunk.id
                neighbor = semantic.get(key)
                if neighbor and neighbor.chunk == chunk:
                    valid.append(neighbor)
                span = chunk.span
                literal = next(
                    (
                        (i, h)
                        for i, h in by_path.get(span.path, ())
                        if (h.start is None and span.start == 1)
                        or (
                            h.start is not None
                            and span.start <= h.start <= span.end
                            and (
                                len(chunk.text) < CHUNK_CHARS
                                and not chunk.column
                                or query.casefold() in chunk.text.casefold()
                            )
                        )
                    ),
                    None,
                )
                if literal:
                    i, hit = literal
                    yield 10000 - i, chunk, hit
                elif words:
                    found = len(
                        words.intersection(re.findall(r"\w+", chunk.text.casefold()))
                    )
                    path_words = len(
                        words.intersection(re.findall(r"\w+", span.path.casefold()))
                    )
                    if found or path_words:
                        yield (
                            4 * found + 2 * path_words + int(span.path == selected),
                            chunk,
                            None,
                        )

        lexical = heapq.nsmallest(
            CANDIDATE_LIMIT,
            candidates(),
            key=lambda c: (-c[0], c[1].span.path, c[1].span.start, c[1].column),
        )
        ranks: dict[str, list[int]] = defaultdict(list)
        items: dict[str, Chunk] = {}
        for rank, (_, chunk, literal_hit) in enumerate(lexical, 1):
            key = chunk.id
            items[key] = chunk
            ranks[key].append(rank)
            if literal_hit:
                labels[key] = literal_hit
        valid.sort(
            key=lambda n: (
                n.distance,
                n.chunk.span.path,
                n.chunk.span.start,
                n.chunk.column,
            )
        )
        lexical_ids = set(items)
        for rank, neighbor in enumerate(valid, 1):
            key = neighbor.chunk.id
            items[key] = neighbor.chunk
            ranks[key].append(rank)

        # Reciprocal rank fusion (k=60). Exact file/symbol navigation takes precedence.
        # Equal scores are ordered by snapshot path, source line and column.
        matches = []
        exact = set()
        term = query.strip().casefold()
        for key, chunk in items.items():
            span = chunk.span
            channel = (
                "Lexical + Semantic"
                if len(ranks[key]) == 2
                else "Lexical"
                if key in lexical_ids
                else "Semantic"
            )
            hit = labels.get(key) or Hit(
                "Source", span.path, chunk.text[:240], span.path, span.start, span.end
            )
            if _exact(hit, term):
                exact.add(key)
            matches.append(
                Match(
                    chunk,
                    replace(hit, channel=channel),
                    sum(1 / (60 + r) for r in ranks[key]),
                )
            )
        matches.sort(
            key=lambda m: (
                m.chunk.id not in exact,
                -m.score,
                m.chunk.span.path,
                m.chunk.span.start,
                m.chunk.column,
            )
        )
        return Retrieval(matches, literals, term)
