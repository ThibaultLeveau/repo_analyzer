import heapq
import re
from collections.abc import Iterator
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from repopilot.grounding import Answer, Evidence, StructuredModel, grounded_answer
from repopilot.search import Chunk, chunks
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.vectors import VectorError, VectorIndex

QUESTION_LIMIT = 1000
SOURCE_LIMIT = 6
_STOP = frozenset(
    ["a", "an", "and", "are", "as", "at", "be", "by", "can", "code", "defined", "do", "does", "file", "for", "from", "how", "i", "in", "is", "it", "me", "of", "on", "or", "project", "repository", "show", "that", "the", "this", "to", "us", "use", "what", "where", "which", "who", "why", "with"]
)
_ANSWER = TypeAdapter(Answer)


def _ranked_chunks(items: Iterator[Chunk], question: str, selected: str) -> list[Chunk]:
    tokens = set(re.findall(r"\w+", question.casefold())) - _STOP
    if not tokens:
        return []

    def matches() -> Iterator[tuple[int, Chunk]]:
        for item in items:
            path = item.span.path.casefold()
            words = set(re.findall(r"\w+", item.text.casefold()))
            found = len(tokens & words)
            in_path = len(tokens & set(re.findall(r"\w+", path)))
            if found or in_path:
                yield 4 * found + 2 * in_path + int(item.span.path == selected), item

    top = heapq.nsmallest(
        SOURCE_LIMIT,
        matches(),
        key=lambda t: (-t[0], t[1].span.path, t[1].span.start, t[1].column),
    )
    return [item for _, item in top]


class QuestionState(TypedDict):
    repository: str
    sha: str
    question: str
    selected: str
    evidence: NotRequired[list[Evidence]]
    answer: NotRequired[Answer]
    answer_id: NotRequired[int]


class Ask:
    def __init__(
        self,
        cache: SnapshotCache,
        store: StateStore,
        vectors: VectorIndex,
        model: StructuredModel | None,
    ) -> None:
        self.cache = cache
        self.store = store
        self.vectors = vectors
        self.model = model
        graph = StateGraph(QuestionState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("answer_question", self._answer)
        graph.add_node("save", self._save)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "answer_question")
        graph.add_edge("answer_question", "save")
        graph.add_edge("save", END)
        self.graph = graph.compile()

    def run(self, repository: str, sha: str, question: str, selected: str = "") -> int:
        if not question.strip() or len(question) > QUESTION_LIMIT:
            raise ValueError(f"Enter a question of 1 to {QUESTION_LIMIT} characters.")
        return int(
            self.graph.invoke(
                {
                    "repository": repository,
                    "sha": sha,
                    "question": question.strip(),
                    "selected": selected,
                }
            )["answer_id"]
        )

    def _retrieve(self, state: QuestionState) -> dict[str, list[Evidence]]:
        repo, sha, question = state["repository"], state["sha"], state["question"]
        snapshot = self.cache.open(repo, sha)
        try:
            candidates = [
                n.chunk for n in self.vectors.nearest(repo, sha, question, SOURCE_LIMIT)
            ]
        except VectorError:
            candidates = []
        if not candidates:
            candidates = _ranked_chunks(
                chunks(snapshot, repo, sha), question, state["selected"]
            )
        evidence: dict[str, Evidence] = {}
        for chunk in candidates:
            span = chunk.span
            lines = snapshot.lines(span.path)
            if not 1 <= span.start <= span.end <= len(lines):
                continue
            text = "\n".join(lines[span.start - 1 : span.end])
            if text[chunk.column : chunk.column + len(chunk.text)] != chunk.text:
                continue
            source = Evidence(repo, sha, span, chunk.text)
            evidence.setdefault(source.id, source)
        return {"evidence": list(evidence.values())}

    def _answer(self, state: QuestionState) -> dict[str, Answer]:
        return {
            "answer": grounded_answer(
                state["question"], state["evidence"], model=self.model
            )
        }

    def _save(self, state: QuestionState) -> dict[str, int]:
        answer = state["answer"]
        ids = list(
            dict.fromkeys(
                source.id for claim in answer.claims for source in claim.sources
            )
        )
        saved_id = self.store.save_question(
            state["repository"],
            state["sha"],
            state["question"],
            _ANSWER.dump_json(answer).decode(),
            ids,
        )
        return {"answer_id": saved_id}

    def saved(self, repository: str, sha: str, answer_id: int) -> tuple[str, Answer]:
        question, payload = self.store.question(repository, sha, answer_id)
        return question, _ANSWER.validate_json(payload)
