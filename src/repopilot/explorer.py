import keyword
import tokenize
from collections import defaultdict
from dataclasses import dataclass
from io import StringIO

from repopilot.snapshots import Entry, SourceSnapshot


@dataclass(frozen=True)
class SourcePage:
    children: dict[str, list[Entry]]
    selected: Entry | None
    lines: list[str]
    first_line: int
    total: int
    highlighting: list[list[tuple[str, str]]]


PAGE_LINES = 200
_STYLES = {
    tokenize.STRING: "string",
    tokenize.FSTRING_START: "string",
    tokenize.FSTRING_MIDDLE: "string",
    tokenize.FSTRING_END: "string",
    tokenize.COMMENT: "comment",
    tokenize.NUMBER: "number",
}


def _style(token: tokenize.TokenInfo) -> str:
    if token.type == tokenize.NAME and keyword.iskeyword(token.string):
        return "keyword"
    return _STYLES.get(token.type, "")


def highlight_python(
    lines: list[str], first: int, stop: int
) -> list[list[tuple[str, str]]]:
    visible = lines[first:stop]
    tokens: list[list[tuple[str, str]]] = [[] for _ in visible]
    columns = [0] * len(visible)
    source = StringIO("\n".join(lines))
    try:
        for token in tokenize.generate_tokens(source.readline):
            start_row, start_col = token.start
            end_row, end_col = token.end
            if start_row > stop:
                break
            if not (style := _style(token)):
                continue
            for row in range(max(first, start_row - 1), min(stop, end_row)):
                local = row - first
                line = visible[local]
                start = start_col if row == start_row - 1 else 0
                end = end_col if row == end_row - 1 else len(line)
                if start > columns[local]:
                    tokens[local].append(("", line[columns[local] : start]))
                if end > start:
                    tokens[local].append((style, line[start:end]))
                columns[local] = end
    except (tokenize.TokenError, SyntaxError):
        pass
    for parts, line, column in zip(tokens, visible, columns, strict=True):
        if column < len(line):
            parts.append(("", line[column:]))
    return tokens


def browse(
    snapshot: SourceSnapshot,
    path: str,
    line: int | None,
    end: int | None,
    page: int | None = None,
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
    if (
        page is not None
        and not 1 <= page <= (len(lines) + PAGE_LINES - 1) // PAGE_LINES
    ):
        raise ValueError("Requested page is outside this file")
    offset = (
        (page - 1) if page is not None else ((line or 1) - 1) // PAGE_LINES
    ) * PAGE_LINES
    stop = min(offset + PAGE_LINES, len(lines))
    if page is None and end is not None and end - (line or 1) < PAGE_LINES:
        stop = max(stop, end)
    highlighting = highlight_python(lines, offset, stop) if path.endswith(".py") else []
    return SourcePage(
        children, selected, lines[offset:stop], offset + 1, len(lines), highlighting
    )
