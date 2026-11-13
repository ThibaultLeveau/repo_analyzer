from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from repopilot.grounding import Answer, Evidence, StructuredModel, grounded_answer
from repopilot.retrieval import HybridRetriever
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.vectors import VectorIndex

QUESTION_LIMIT = 1000
_ANSWER = TypeAdapter(Answer)


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
        self.store = store
        self.retriever = HybridRetriever(cache, vectors)
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
        result = self.retriever.retrieve(repo, sha, question, state["selected"])
        return {"evidence": result.evidence()}

    def _answer(self, state: QuestionState) -> dict[str, Answer]:
        return {
            "answer": grounded_answer(
                state["question"], state["evidence"], model=self.model
            )
        }

    def _save(self, state: QuestionState) -> dict[str, int]:
        return {
            "answer_id": self.save(
                state["repository"], state["sha"], state["question"], state["answer"]
            )
        }

    def save(self, repository: str, sha: str, question: str, answer: Answer) -> int:
        ids = list(
            dict.fromkeys(
                source.id for claim in answer.claims for source in claim.sources
            )
        )
        return self.store.save_question(
            repository,
            sha,
            question,
            _ANSWER.dump_json(answer).decode(),
            ids,
        )

    def saved(self, repository: str, sha: str, answer_id: int) -> tuple[str, Answer]:
        question, payload = self.store.question(repository, sha, answer_id)
        return question, _ANSWER.validate_json(payload)
