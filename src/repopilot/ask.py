import re
import sqlite3
from dataclasses import replace
from typing import NotRequired, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import TypeAdapter

from repopilot.grounding import (
    Answer,
    Claim,
    DocumentationEvidence,
    Evidence,
    NoteEvidence,
    Source,
    StructuredModel,
    grounded_answer,
)
from repopilot.history import History
from repopilot.official_docs import DEFAULTS, OfficialDocs
from repopilot.python_facts import Span
from repopilot.rerank import MAX_CANDIDATES, Reranker
from repopilot.retrieval import HybridRetriever
from repopilot.routing import Route, budget, document_query, passage, route
from repopilot.search import CHUNK_CHARS, chunks
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.timeline import explains, timeline
from repopilot.user_notes import Notes
from repopilot.vectors import VectorIndex

QUESTION_LIMIT = 1000
_ANSWER = TypeAdapter(Answer)


class QuestionState(TypedDict):
    repository: str
    sha: str
    question: str
    selected: str
    target: str
    route: NotRequired[Route]
    notices: NotRequired[tuple[str, ...]]
    evidence: NotRequired[list[Source]]
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
        docs: OfficialDocs | None = None,
        reranker: Reranker | None = None,
        notes: Notes | None = None,
    ) -> None:
        self.store = store
        self.retriever = HybridRetriever(cache, vectors)
        self.model = model
        self.history = history
        self.docs = docs
        self.reranker = reranker
        self.notes = notes
        graph = StateGraph(QuestionState)
        graph.add_node("route_question", self._route)
        graph.add_node("retrieve", self._retrieve)
        graph.add_node("answer_question", self._answer)
        graph.add_node("save", self._save)
        graph.add_edge(START, "route_question")
        graph.add_edge("route_question", "retrieve")
        if reranker:
            graph.add_node("rerank", self._rerank)
            graph.add_edge("retrieve", "rerank")
            graph.add_edge("rerank", "answer_question")
        else:
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

    def _route(self, state: QuestionState) -> dict[str, Route]:
        catalog = tuple(self.docs.catalog if self.docs else DEFAULTS)
        return {"route": route(state["question"], state["target"], catalog)}

    def _history(self, state: QuestionState) -> tuple[list[Source], Answer | None]:
        repo, sha, question = state["repository"], state["sha"], state["question"]
        result = timeline(self.history, repo, sha, question, state["target"])
        if result.unavailable:
            return [], result.unavailable
        sources: list[Source] = list(result.evidence)
        if not result.paths:
            return sources, None
        snapshot = self.retriever.cache.open(repo, sha)
        terms = set(
            re.findall(
                r"\w{4,}",
                (question + " " + " ".join(e.excerpt for e in result.evidence)).lower(),
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
            source for _, source in sorted(best.values(), key=lambda item: -item[0])[:2]
        )
        return sources, None

    def _retrieve(self, state: QuestionState) -> dict[str, object]:
        repo, sha, question = state["repository"], state["sha"], state["question"]
        self.retriever.cache.open(repo, sha)
        plan = state["route"]
        groups: list[list[Source]] = []
        notices: list[str] = []
        failure = Answer("insufficient_evidence")
        if plan.notes:
            query = document_query(question, ("user", "project", "local", "uploaded", "attached", "markdown", "my", "note", "notes"))
            try:
                matches = self.notes.search(repo, query) if self.notes else []
            except (TimeoutError, sqlite3.OperationalError):
                matches = []
            groups.append(
                [
                    NoteEvidence(
                        repo,
                        sha,
                        note.id,
                        note.revision,
                        note.filename,
                        part.section,
                        note.url(repo, sha) + (part.url or "#user-notes"),
                        note.uploaded,
                        passage(part.text, query),
                    )
                    for note, part in matches
                ]
            )
            if not matches:
                failure = Answer("notes_unavailable")
                notices.append("No matching current user note is available.")
        if plan.history:
            try:
                history, error = self._history(state)
            except (TimeoutError, sqlite3.OperationalError):
                history, error = [], Answer("history_unavailable")
            groups.append(history)
            if error:
                failure = error
                notices.append("No verified GitHub history for this reference.")
        if plan.docs:
            try:
                query = document_query(question, plan.packages)
                hits = (
                    self.docs.search(repo, sha, query, packages=plan.packages)
                    if self.docs
                    else []
                )
            except (TimeoutError, sqlite3.OperationalError):
                hits = []
            groups.append(
                [
                    DocumentationEvidence(
                        repo,
                        sha,
                        pkg,
                        ver,
                        ed,
                        part.title,
                        part.section,
                        part.url,
                        fetched,
                        passage(part.text, query),
                    )
                    for pkg, ver, ed, fetched, part in hits
                ]
            )
            if not hits:
                if not plan.history:
                    failure = Answer("docs_unavailable")
                notices.append(
                    "No matching, current official documentation is indexed for this question."
                )
        if plan.code:
            try:
                result = self.retriever.retrieve(repo, sha, question, state["selected"])
                code = (
                    result.evidence(
                        limit=MAX_CANDIDATES, char_limit=MAX_CANDIDATES * CHUNK_CHARS
                    )
                    if self.reranker
                    else result.evidence()
                )
            except (TimeoutError, sqlite3.OperationalError):
                code = []
            groups.append(list(code))
            if not code:
                notices.append("No matching repository code was available.")
        evidence = (
            budget(
                groups, limit=MAX_CANDIDATES, char_limit=MAX_CANDIDATES * CHUNK_CHARS
            )
            if self.reranker
            else budget(groups)
        )
        if not evidence:
            return {
                "answer": failure,
                "evidence": [],
                "notices": tuple(notices) if len(groups) > 1 else (),
            }
        return {"evidence": evidence, "notices": tuple(notices)}

    def _rerank(self, state: QuestionState) -> dict[str, list[Source]]:
        candidates = state["evidence"]
        ranked = (
            self.reranker.rank(state["question"], candidates)
            if self.reranker
            else candidates
        )
        return {"evidence": budget([ranked])}

    def _answer(self, state: QuestionState) -> dict[str, Answer]:
        if "answer" in state:
            return {
                "answer": replace(state["answer"], notices=state.get("notices", ()))
            }
        answer = grounded_answer(
            state["question"],
            state["evidence"],
            model=self.model,
            unavailable=state.get("notices", ()),
        )
        plan = state["route"]
        for claim in answer.claims:
            if not self._supported(plan, state["question"], claim):
                answer = Answer("insufficient_evidence")
                break
        return {"answer": replace(answer, notices=state.get("notices", ()))}

    @staticmethod
    def _supported(plan: Route, question: str, claim: Claim) -> bool:
        if claim.notes:
            return plan.notes
        if claim.history:
            return explains(question, claim.text)
        if plan.history and not (plan.code or plan.docs):
            return False
        if claim.documentation:
            return True
        if plan.docs and re.search(
            r"\b(official|publisher|documentation|docs)\b", claim.text, re.IGNORECASE
        ):
            return False
        return not (
            plan.history
            and re.search(
                r"\b(reverted|rejected|closed|merged)\b", claim.text, re.IGNORECASE
            )
        )

    def _save(self, state: QuestionState) -> dict[str, int]:
        return {
            "answer_id": self.save(
                state["repository"], state["sha"], state["question"], state["answer"]
            )
        }

    def save(self, repository: str, sha: str, question: str, answer: Answer) -> int:
        ids = dict.fromkeys(
            source.id for claim in answer.claims for source in claim.evidence
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
