from collections import defaultdict
from dataclasses import dataclass

from repopilot.python_facts import Span, Symbol
from repopilot.references import Reference, ReferenceIndex

MAX_DEPTH = 4
MAX_CALLS = 48


@dataclass(frozen=True)
class CallSite:
    caller: str
    destination: str
    span: Span
    depth: int
    parent_row: int
    boundary: str


@dataclass(frozen=True)
class CallFlow:
    entry: Symbol
    calls: list[CallSite]
    truncated: bool


def trace(index: ReferenceIndex, path: str, entry: str) -> CallFlow:
    root = index.definitions.get(entry)
    if not root or root.kind not in {"function", "method"} or root.span.path != path:
        raise KeyError("Entry point unavailable")
    outgoing: dict[str, list[Reference]] = defaultdict(list)
    for ref in index.refs:
        owner = index.definitions.get(ref.caller)
        if (
            ref.kind == "call"
            and owner
            and owner.span.path == ref.span.path
            and owner.span.start <= ref.span.start <= owner.span.end
        ):
            outgoing[ref.caller].append(ref)
    calls: list[CallSite] = []
    truncated = False

    def visit(caller: Symbol, ancestry: tuple[str, ...], parent: int) -> None:
        nonlocal truncated
        for ref in outgoing[caller.name]:
            if len(calls) >= MAX_CALLS:
                truncated = True
                return
            target = ref.target
            resolved = target if isinstance(target, Symbol) else None
            if not resolved:
                boundary = "Unresolved"
            elif resolved.kind not in {"function", "method"}:
                boundary = "Constructor or non-function target"
            elif resolved.name in ancestry:
                boundary = "Cycle"
            elif len(ancestry) >= MAX_DEPTH and outgoing[resolved.name]:
                boundary = "Depth limit"
            else:
                boundary = ""
            calls.append(
                CallSite(
                    caller.name,
                    resolved.name if resolved else ref.text,
                    ref.span,
                    len(ancestry),
                    parent,
                    boundary,
                )
            )
            if resolved and not boundary and len(ancestry) < MAX_DEPTH:
                visit(resolved, (*ancestry, resolved.name), len(calls))

    visit(root, (root.name,), 0)
    return CallFlow(root, calls, truncated)
