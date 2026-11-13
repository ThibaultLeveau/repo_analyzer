from dataclasses import dataclass
from urllib.parse import urlencode

from repopilot.history import Observation
from repopilot.issues import IssueQueue
from repopilot.search import RESULT_LIMIT, Hit
from repopilot.state import Snapshot


@dataclass(frozen=True)
class Destination:
    label: str
    url: str
    detail: str


def grouped(
    repo: str,
    sha: str,
    query: str,
    hits: list[Hit],
    queue: IssueQueue,
    observations: list[Observation],
    snapshots: list[Snapshot],
) -> dict[str, list[Destination]]:
    base = f"/repos/{repo}/{sha}"
    groups: dict[str, list[Destination]] = {}
    for hit in hits:
        args: dict[str, str | int] = {"path": hit.path}
        if hit.start is not None and hit.end is not None:
            args.update(line=hit.start, end=hit.end)
        link = (
            base + "/code?" + urlencode(args) + (f"#L{hit.start}" if hit.start else "")
        )
        group = {"File": "Files", "Symbol": "Symbols", "Source": "Source text"}[
            hit.kind
        ]
        detail = hit.path + (f" · L{hit.start}–{hit.end}" if hit.start else "")
        groups.setdefault(group, []).append(Destination(hit.label, link, detail))
    term = query.strip().casefold()
    tickets: dict[int, Destination] = {}
    commits: dict[str, Destination] = {}
    for item in queue.issues:
        if term in f"#{item.number} {item.title} {item.body}".casefold():
            tickets[item.number] = Destination(
                f"#{item.number} {item.title}",
                base + f"/issues?number={item.number}",
                repo,
            )
    for observation in observations:
        for record in observation.records:
            if (
                term
                not in f"{record.key} #{record.number or ''} {record.title} {record.body}".casefold()
            ):
                continue
            if record.kind == "commit":
                commits[record.id] = Destination(record.title, record.url, record.id)
            elif record.kind == "issue" and record.number:
                tickets.setdefault(
                    record.number,
                    Destination(f"#{record.number} {record.title}", record.url, repo),
                )
    for snap in snapshots:
        if term in snap.sha:
            commits[snap.sha] = Destination(
                snap.sha[:12], f"/repos/{repo}/{snap.sha}/architecture", snap.sha
            )
    if tickets:
        groups["Issues"] = list(tickets.values())[:RESULT_LIMIT]
    if commits:
        groups["Commits"] = list(commits.values())[:RESULT_LIMIT]
    return groups
