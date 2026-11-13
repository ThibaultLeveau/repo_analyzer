import re
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from repopilot.grounding import (
    Answer,
    Evidence,
    HistoricalEvidence,
    StructuredModel,
    grounded_answer,
)
from repopilot.history import History
from repopilot.python_facts import Span
from repopilot.retrieval import HybridRetriever
from repopilot.search import chunks
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.timeline import explains, timeline, timeline_requested
from repopilot.vectors import VectorIndex

QUESTION_LIMIT = 1000
_ANSWER = TypeAdapter(Answer)


class QuestionState(TypedDict):
    repository: str
    sha: str
    question: str
    selected: str
    target: str
    evidence: NotRequired[list[Evidence | HistoricalEvidence]]
    answer: NotRequired[Answer]
    answer_id: NotRequired[int]


class Ask:
    def __init__(
        self,
        cache: SnapshotCache,
        store: StateStore,
        vectors: VectorIndex,
        model: StructuredModel | None,
        history: History | None = None,
    ) -> None:
        self.store = store
        self.retriever = HybridRetriever(cache, vectors)
        self.model = model
        self.history = history
        graph = StateGraph(QuestionState)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("answer_question", self._answer)
        graph.add_node("save", self._save)
        graph.add_edge(START, "retrieve")
        graph.add_edge("retrieve", "answer_question")
        graph.add_edge("answer_question", "save")
        graph.add_edge("save", END)
        self.graph = graph.compile()

    def run(
        self,
        repository: str,
        sha: str,
        question: str,
        selected: str = "",
        target: str = "",
    ) -> int:
        if not question.strip() or len(question) > QUESTION_LIMIT:
            raise ValueError(f"Enter a question of 1 to {QUESTION_LIMIT} characters.")
        return int(
            self.graph.invoke(
                {
                    "repository": repository,
                    "sha": sha,
                    "question": question.strip(),
                    "selected": selected,
                    "target": target.strip(),
                }
            )["answer_id"]
        )

    def _retrieve(self, state: QuestionState) -> dict[str, object]:
        repo, sha, question = state["repository"], state["sha"], state["question"]
        if timeline_requested(question, state["target"]):
            snapshot = self.retriever.cache.open(repo, sha)
            result = timeline(self.history, repo, sha, question, state["target"])
            if result.unavailable:
                return {"answer": result.unavailable, "evidence": []}
            sources: list[Evidence | HistoricalEvidence] = list(result.evidence)
            terms = set(
                re.findall(
                    r"\w{4,}",
                    (
                        question + " " + " ".join(e.excerpt for e in result.evidence)
                    ).lower(),
                )
            )
            best: dict[str, tuple[int, Evidence]] = {}
            for chunk in chunks(snapshot, repo, sha):
                path = chunk.span.path
                if path not in result.paths:
                    continue
                weight = len(terms.intersection(re.findall(r"\w+", chunk.text.lower())))
                if path not in best or weight > best[path][0]:
                    excerpt = chunk.text[:1000]
                    span = Span(
                        path, chunk.span.start, chunk.span.start + excerpt.count("\n")
                    )
                    best[path] = weight, Evidence(repo, sha, span, excerpt)
            sources.extend(
                source
                for _, source in sorted(best.values(), key=lambda item: -item[0])[:2]
            )
            return {"evidence": sources}
        return {
            "evidence": self.retriever.retrieve(
                repo, sha, question, state["selected"]
            ).evidence()
        }

    def _answer(self, state: QuestionState) -> dict[str, Answer]:
        if "answer" in state:
            return {"answer": state["answer"]}
        answer = grounded_answer(state["question"], state["evidence"], model=self.model)
        for claim in answer.claims:
            if claim.history and not explains(state["question"], claim.text):
                return {"answer": Answer("insufficient_evidence")}
        return {"answer": answer}

    def _save(self, state: QuestionState) -> dict[str, int]:
        return {
            "answer_id": self.save(
                state["repository"], state["sha"], state["question"], state["answer"]
            )
        }

    def save(self, repository: str, sha: str, question: str, answer: Answer) -> int:
        ids = dict.fromkeys(
            source.id for claim in answer.claims for source in claim.sources
        )
        ids.update(
            dict.fromkeys(
                source.id for claim in answer.claims for source in claim.history
            )
        )
        return self.store.save_question(
            repository,
            sha,
            question,
            _ANSWER.dump_json(answer).decode(),
            list(ids),
        )

    def saved(self, repository: str, sha: str, answer_id: int) -> tuple[str, Answer]:
        question, payload = self.store.question(repository, sha, answer_id)
        return question, _ANSWER.validate_json(payload)
