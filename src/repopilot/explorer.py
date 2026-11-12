from collections import defaultdict
from dataclasses import dataclass

from repopilot.snapshots import Entry, SourceSnapshot


@dataclass(frozen=True)
class SourcePage:
    children: dict[str, list[Entry]]
    selected: Entry | None
    lines: list[str]
    first_line: int
    total: int


PAGE_LINES = 200


def browse(
    snapshot: SourceSnapshot, path: str, line: int | None, end: int | None
) -> SourcePage:
    children: dict[str, list[Entry]] = defaultdict(list)
    selected = None
    for entry in snapshot.tree():
        children[entry.path.rpartition("/")[0]].append(entry)
        if entry.path == path:
            selected = entry
    for entries in children.values():
        entries.sort(key=lambda e: (e.kind != "directory", e.path.casefold(), e.path))
    if path and selected is None:
        raise FileNotFoundError(path)

    lines = []
    if selected and selected.kind == "text":
        lines = snapshot.lines(path)
    if (end is not None and line is None) or (
        line is not None
        and not 1 <= line <= (end if end is not None else line) <= len(lines)
    ):
        raise ValueError("Requested line range is outside this file")
    offset = ((line or 1) - 1) // PAGE_LINES * PAGE_LINES
    return SourcePage(
        children, selected, lines[offset : offset + PAGE_LINES], offset + 1, len(lines)
    )
