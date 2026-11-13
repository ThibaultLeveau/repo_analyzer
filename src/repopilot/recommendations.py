import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from repopilot.history import HistoryRecord, Observation
from repopilot.issues import Issue
from repopilot.python_facts import PythonFacts
from repopilot.scoring import Assessment, static_assessment
from repopilot.snapshots import SourceSnapshot

_COMMON = {
    "with",
    "this",
    "that",
    "from",
    "issue",
    "should",
    "could",
    "would",
    "please",
    "needs",
    "change",
    "update",
    "implement",
    "tests",
    "test",
    "code",
    "file",
    "files",
    "when",
    "using",
    "into",
    "where",
    "also",
    "have",
    "more",
    "work",
    "branch",
    "request",
    "add",
    "fix",
    "for",
    "and",
    "the",
}
_BLOCKED = {
    "blocked",
    "needs discussion",
    "needs design",
    "needs maintainer",
    "wontfix",
    "invalid",
    "duplicate",
    "breaking change",
}


@dataclass(frozen=True)
class Precedent:
    issue: HistoryRecord
    changes: tuple[HistoryRecord, ...]
    terms: frozenset[str]


@dataclass
class Recommendation:
    issue: Issue
    assessment: Assessment
    precedent: Precedent
    shared: list[str]
    overlap: list[str]
    confidence: str
    relevance: int


@dataclass
class Ranking:
    recommended: list[Recommendation] = field(default_factory=list)
    uncertain: list[tuple[Issue, str]] = field(default_factory=list)


def _terms(text: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z][a-z0-9_]{2,}", text.lower())) - _COMMON


def precedents(
    repo: str, sha: str, observations: Sequence[Observation]
) -> list[Precedent]:
    records: dict[str, HistoryRecord] = {}
    related: dict[str, set[str]] = {}
    for batch in sorted(observations, key=lambda o: o.observed_at):
        if batch.repository.lower() != repo.lower() or batch.sha != sha:
            continue
        records.update((r.key, r) for r in batch.records)
        for link in batch.links:
            if link.relation == "closed_by_commit":
                related.setdefault(link.source, set()).add(link.target)
            elif link.relation == "references" and link.source.startswith("pull:"):
                related.setdefault(link.target, set()).add(link.source)
    result = []
    for row in records.values():
        if (
            row.kind != "issue"
            or row.state != "closed"
            or _BLOCKED.intersection(label.casefold() for label in row.labels)
        ):
            continue
        changes = tuple(
            records[k]
            for k in sorted(related.get(row.key, ()))
            if k in records
            and (records[k].kind == "commit" or records[k].state == "merged")
        )
        if changes:
            result.append(Precedent(row, changes, _terms(row.title + " " + row.body)))
    return result


def recommend(
    snapshot: SourceSnapshot,
    facts: PythonFacts,
    repo: str,
    sha: str,
    issues: Sequence[Issue],
    observations: Sequence[Observation],
) -> Ranking:
    history = precedents(repo, sha, observations)
    ranking = Ranking()
    for issue in issues:
        terms = _terms(issue.title + " " + issue.body)
        labels = {label.casefold() for label in issue.labels}
        matches = [
            p
            for p in history
            if p.issue.number != issue.number and len(terms & p.terms) >= 2
        ]
        reason = ""
        if labels & _BLOCKED:
            reason = (
                "A maintainer label requires clarification: "
                + ", ".join(sorted(labels & _BLOCKED))
                + "."
            )
        elif not matches:
            reason = "No similar closed issue has a recorded fix or a linked merged pull request in the cached history."
        if reason:
            ranking.uncertain.append((issue, reason))
            continue
        assessment = static_assessment(snapshot, facts, repo, sha, issue)
        if (
            assessment.score is None
            or assessment.confidence != "High"
            or not assessment.evidence
        ):
            ranking.uncertain.append(
                (
                    issue,
                    "A unique component and matching test files could not be verified for the full scope.",
                )
            )
            continue
        if assessment.difficulty == "High":
            ranking.uncertain.append(
                (
                    issue,
                    "The static estimate indicates a large change. Ask a maintainer to narrow the scope.",
                )
            )
            continue
        paths = {e.span.path for e in assessment.evidence}

        options = []
        for precedent in matches:
            overlap = sorted(
                paths.intersection(
                    path for change in precedent.changes for path in change.paths
                )
            )
            shared = sorted(terms & precedent.terms)
            labels_in_common = labels.intersection(
                label.casefold() for label in precedent.issue.labels
            )
            relevance = (
                2 * min(len(overlap), 3)
                + min(len(shared), 4)
                + min(len(labels_in_common), 2)
            )
            options.append(
                Recommendation(
                    issue,
                    assessment,
                    precedent,
                    shared,
                    overlap,
                    "High" if overlap else "Moderate",
                    relevance,
                )
            )
        ranking.recommended.append(
            max(options, key=lambda r: (r.relevance, -(r.precedent.issue.number or 0)))
        )
    ranking.recommended.sort(
        key=lambda r: (-(r.relevance / (1 + (r.assessment.score or 0))), r.issue.number)
    )
    ranking.uncertain.sort(key=lambda pair: pair[0].number)
    return ranking
