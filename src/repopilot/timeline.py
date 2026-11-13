import re
from dataclasses import dataclass

from repopilot.grounding import Answer, HistoricalEvidence
from repopilot.history import History, HistoryRecord

_ISSUE = re.compile(
    r"(?:(?P<pull>\bpr|\bpull request)|\bissue)?\s*#(?P<id>[1-9]\d*)\b", re.IGNORECASE
)
_COMMIT = re.compile(r"\b[a-f0-9]{7,40}\b", re.IGNORECASE)
_TIMELINE = re.compile(
    r"\b(timeline|history of|what changed|which commit|when was .+ (merged|closed|reverted))\b",
    re.IGNORECASE,
)
_REASON = re.compile(
    r"\b(why|reason|motive|motivation|what (?:caused|prompted))\b", re.IGNORECASE
)
_EXPLANATION = re.compile(
    r"\b(because|due to|since|reason|in order to|so that|caused|broke|breaks|failed|fails|regression|to (?:fix|prevent|avoid|restore|preserve|support))\b",
    re.IGNORECASE,
)
_ACTIONS = (
    re.compile(r"\b(revert\w*|roll(?:ed)? back)\b", re.IGNORECASE),
    re.compile(r"\b(reject\w*|declin\w*|won't merge|will not merge)\b", re.IGNORECASE),
    re.compile(r"\b(clos(?:e|ed|ing)|wontfix|won't fix)\b", re.IGNORECASE),
)


@dataclass
class Timeline:
    evidence: list[HistoricalEvidence]
    paths: list[str]
    unavailable: Answer | None = None


def timeline_requested(question: str, target: str) -> bool:
    return bool(
        target
        or _ISSUE.search(question)
        or _COMMIT.search(question)
        or _TIMELINE.search(question)
    )


def explains(question: str, statement: str) -> bool:
    if not _REASON.search(question):
        return True
    action = next((pattern for pattern in _ACTIONS if pattern.search(question)), None)
    return bool(
        _EXPLANATION.search(statement) and (action is None or action.search(statement))
    )


def timeline(
    history: History | None, repo: str, sha: str, question: str, target: str
) -> Timeline:
    observations = history.observations(repo, sha) if history else []
    if not observations:
        return Timeline([], [], Answer("history_unavailable"))
    records: dict[str, tuple[HistoryRecord, float]] = {}
    links: dict[str, set[str]] = {}
    for observation in sorted(observations, key=lambda o: o.observed_at):
        for record in observation.records:
            records[record.key] = record, observation.observed_at
        for edge in observation.links:
            links.setdefault(edge.source, set()).add(edge.target)
            links.setdefault(edge.target, set()).add(edge.source)

    selection = target or question
    reference = _ISSUE.search(selection)
    commit = _COMMIT.search(selection)
    if reference:
        key = ("pull:" if reference["pull"] else "issue:") + reference["id"]
    elif commit:
        matches = [
            k
            for k, (row, _) in records.items()
            if row.kind == "commit" and row.id.startswith(commit[0].lower())
        ]
        if len(matches) > 1:
            return Timeline([], [], Answer("ambiguous_revision"))
        key = matches[0] if matches else "unknown"
    elif target and target.lower() != "timeline":
        raise ValueError(
            "Enter an issue number with #, a PR number with PR #, a commit SHA, or timeline."
        )
    else:
        key = ""
    if key and key not in records:
        return Timeline([], [], Answer("history_unavailable"))
    if key:
        related = {key} | links.get(key, set())
        # A commit connected to a PR can obtain the discussion through the referenced issue.
        frontier = set(related)
        for _ in range(2):
            frontier = {
                other for node in frontier for other in links.get(node, ())
            } - related
            related.update(frontier)
        rows = [records[k] for k in related if k in records]
    else:
        rows = list(records.values())
    terms = {w for w in re.findall(r"[a-z0-9_]+", question.lower()) if len(w) > 3}
    rows.sort(
        key=lambda entry: (
            entry[0].key == key,
            len(
                terms.intersection(
                    re.findall(
                        r"[a-z0-9_]+", (entry[0].title + " " + entry[0].body).lower()
                    )
                )
            ),
            entry[0].updated_at,
            entry[0].key,
        ),
        reverse=True,
    )
    evidence = []
    paths = list(dict.fromkeys(path for r, _ in rows for path in r.paths))[:6]
    for row, observed in rows:
        label = f"{row.kind.capitalize()} {row.id if row.number is None else '#' + str(row.number)}"
        text = "\n".join(dict.fromkeys(part for part in (row.title, row.body) if part))
        if row.state:
            text = f"{label} was {row.state}.\n{text}"
        text = "\n".join(
            s.strip()
            for s in re.split(r"(?<=[.!?])\s+|\n+", text)
            if s.strip() and explains(question, s)
        )
        if text:
            evidence.append(
                HistoricalEvidence(
                    repo,
                    sha,
                    label,
                    row.url,
                    row.timestamp,
                    row.updated_at,
                    observed,
                    row.author,
                    text[:850],
                )
            )
        if len(evidence) == 4:
            break
    return Timeline(
        evidence, paths, None if evidence else Answer("insufficient_evidence")
    )
