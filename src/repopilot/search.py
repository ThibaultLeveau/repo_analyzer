import hashlib
import heapq
import json
import re
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

from repopilot.languages import analyze_snapshot
from repopilot.python_facts import Span, Symbol
from repopilot.snapshots import SourceSnapshot

CHUNK_CHARS = 1600
CHUNK_LINES = 40
QUERY_LIMIT = 160
RESULT_LIMIT = 40


@dataclass(frozen=True)
class Chunk:
    repository: str
    sha: str
    span: Span
    column: int
    text: str

    @property
    def id(self) -> str:
        location = [
            self.repository.lower(),
            self.sha,
            self.span.path,
            self.span.start,
            self.column,
            self.text,
        ]
        return hashlib.sha256(
            json.dumps(location, ensure_ascii=False).encode()
        ).hexdigest()


def _ranges(lines: list[str]) -> Iterator[tuple[int, int, int, str]]:
    start = 0
    while start < len(lines):
        line = lines[start]
        if len(line) > CHUNK_CHARS:
            for col in range(0, len(line), CHUNK_CHARS):
                yield start + 1, start + 1, col, line[col : col + CHUNK_CHARS]
            start += 1
            continue
        size = len(line)
        end = start + 1
        while end < min(len(lines), start + CHUNK_LINES):
            size += len(lines[end]) + 1
            if size > CHUNK_CHARS:
                break
            end += 1
        yield start + 1, end, 0, "\n".join(lines[start:end])
        start = end


def chunks(snapshot: SourceSnapshot, repository: str, sha: str) -> Iterator[Chunk]:
    for path, content in snapshot.texts():
        lines = content.removesuffix("\n").split("\n")
        for first, last, column, text in _ranges(lines):
            if text.strip():
                yield Chunk(repository, sha, Span(path, first, last), column, text)


@dataclass(frozen=True)
class Hit:
    kind: Literal["File", "Symbol", "Source"]
    label: str
    snippet: str
    path: str
    start: int | None = None
    end: int | None = None
    channel: str = "Lexical"


def _snippet(line: str, match: int = 0) -> str:
    start = max(0, match - 40)
    end = start + QUERY_LIMIT + 80
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


class LexicalSearch:
    def __init__(self, snapshot: SourceSnapshot) -> None:
        self.snapshot = snapshot
        self.paths = [e.path for e in snapshot.tree() if e.kind != "directory"]
        self.symbols: dict[str, list[Symbol]] = defaultdict(list)
        for module in analyze_snapshot(snapshot).modules:
            self.symbols[module.span.path].extend(module.symbols)

    def search(self, query: str, *, limit: int = RESULT_LIMIT) -> list[Hit]:
        if len(query) > QUERY_LIMIT:
            raise ValueError(f"Enter at most {QUERY_LIMIT} characters.")
        term = query.strip()
        if not term or limit <= 0:
            return []
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        ranked = heapq.nsmallest(
            min(limit, RESULT_LIMIT),
            self._matches(pattern),
            key=lambda pair: (pair[0], pair[1].path, pair[1].start or 0, pair[1].label),
        )
        return [hit for _, hit in ranked]

    def _matches(self, pattern: re.Pattern[str]) -> Iterator[tuple[int, Hit]]:
        for path in self.paths:
            if pattern.search(path):
                exact = pattern.fullmatch(path) or pattern.fullmatch(
                    path.rsplit("/", 1)[-1]
                )
                yield (0 if exact else 3), Hit("File", path, "", path)
        for path, content in self.snapshot.texts():
            lines = content.removesuffix("\n").split("\n")
            definitions: set[int] = set()
            for symbol in self.symbols.get(path, []):
                if pattern.search(symbol.name):
                    span = symbol.span
                    exact = pattern.fullmatch(symbol.name) or pattern.fullmatch(
                        symbol.name.rsplit(".", 1)[-1]
                    )
                    definitions.add(span.start)
                    yield (
                        (1 if exact else 2),
                        Hit(
                            "Symbol",
                            symbol.name,
                            _snippet(lines[span.start - 1]),
                            path,
                            span.start,
                            span.end,
                        ),
                    )
            for number, line in enumerate(lines, 1):
                if number not in definitions and (match := pattern.search(line)):
                    yield (
                        4,
                        Hit(
                            "Source",
                            path,
                            _snippet(line, match.start()),
                            path,
                            number,
                            number,
                        ),
                    )
