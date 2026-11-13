import ast
import re
from collections.abc import Iterator
from dataclasses import dataclass

from repopilot.audit import Finding
from repopilot.patches import PatchPreview, source_diff
from repopilot.python_facts import Module, Span
from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class Candidate:
    function: ast.FunctionDef
    argument: ast.arg
    default: ast.List | ast.Dict
    span: Span

    @property
    def title(self) -> str:
        return f"Mutable default for {self.function.name}({self.argument.arg})"


def _candidates(module: Module, tree: ast.Module) -> Iterator[Candidate]:
    functions = {s.span.start for s in module.symbols if s.kind == "function"}
    for fn in tree.body:
        if (
            not isinstance(fn, ast.FunctionDef)
            or fn.decorator_list
            or fn.lineno not in functions
        ):
            continue
        args = fn.args
        positional = [*args.posonlyargs, *args.args]
        defaults = [
            *zip(
                positional[len(positional) - len(args.defaults) :],
                args.defaults,
                strict=True,
            ),
            *zip(args.kwonlyargs, args.kw_defaults, strict=True),
        ]
        for arg, value in defaults:
            if isinstance(value, (ast.List, ast.Dict)):
                yield Candidate(
                    fn,
                    arg,
                    value,
                    Span(
                        module.span.path, value.lineno, value.end_lineno or value.lineno
                    ),
                )


def maintenance_findings(snapshot: SourceSnapshot, module: Module) -> list[Finding]:
    if not module.span.path.endswith(".py"):
        return []
    tree = ast.parse(snapshot.read_text(module.span.path))
    return [
        Finding(
            "BUG001",
            "warning",
            item.title,
            "Python creates this default container once when defining the function.",
            "Sharing can be intentional. A correction requires an explicit per-call contract.",
            item.span,
        )
        for item in _candidates(module, tree)
    ]


def _replacement(text: str, item: Candidate) -> str | None:
    fn, arg, default = item.function, item.argument.arg, item.default
    literal, kind = ("[]", "list") if isinstance(default, ast.List) else ("{}", "dict")
    doc = ast.get_docstring(fn) or ""
    if re.search(r"\b(?:shared|cache\w*|memoiz\w*|persist\w*)\b", doc, re.IGNORECASE):
        return None
    contract = rf"(?:if|when) {re.escape(arg)} is (?:omitted or none|none or omitted), (?:create|use) a (?:fresh|new) {kind} (?:for|on) each call[.]"
    if not any(
        re.fullmatch(contract, line.strip(), re.IGNORECASE) for line in doc.splitlines()
    ):
        return None
    if (
        ast.get_source_segment(text, default) != literal
        or item.argument.annotation
        or fn.type_comment
        or len(fn.body) < 2
        or len(set(re.findall(r"\r\n|\r|\n", text))) != 1
    ):
        return None
    first = fn.body[1]
    if (
        isinstance(first, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and first.decorator_list
    ):
        return None
    if (
        first.lineno <= (fn.body[0].end_lineno or fn.lineno)
        or not default.end_col_offset
        or "\r" in text.replace("\r\n", "")
    ):
        return None
    nl = "\r\n" if "\r\n" in text else "\n"
    parts = text.split(nl)
    lines = [part + nl for part in parts[:-1]] + [parts[-1]]
    body = lines[first.lineno - 1]
    indent = body[: len(body) - len(body.lstrip(" \t"))]
    if not indent or first.col_offset != len(indent):
        return None
    # AST columns count UTF-8 bytes, including in a non-ASCII signature.
    raw = lines[default.lineno - 1].encode()
    lines[default.lineno - 1] = (
        raw[: default.col_offset] + b"None" + raw[default.end_col_offset :]
    ).decode()
    lines.insert(
        first.lineno - 1,
        f"{indent}if {arg} is None:{nl}{indent}{indent}{arg} = {literal}{nl}",
    )
    after = "".join(lines)
    try:
        ast.parse(after)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return after


def maintenance_fix(
    snapshot: SourceSnapshot,
    module: Module,
    finding: Finding,
    expected_sha: str,
    *,
    proposed: str | None = None,
) -> PatchPreview:
    if (
        snapshot.database.parent.name != expected_sha
        or finding.rule != "BUG001"
        or module.span.path != finding.span.path
    ):
        return PatchPreview("The finding must match the pinned source revision.")
    path = finding.span.path
    text = snapshot.read_text(path)
    candidates = _candidates(module, ast.parse(text))
    item = next(
        (c for c in candidates if c.span == finding.span and c.title == finding.title),
        None,
    )
    after = _replacement(text, item) if item else None
    if after is None:
        return PatchPreview(
            "No verified correction. This edit requires an empty unannotated default and an explicit docstring contract for omitted and None arguments."
        )
    size = next(e.size for e in snapshot.tree() if e.path == path)
    bom = "\ufeff" if size == len(text.encode()) + 3 else ""
    validated = source_diff(path, bom + text, bom + after)
    if proposed is not None and proposed != validated:
        return PatchPreview(
            "The proposed patch differs from the validated path, hunk or replacement."
        )
    return PatchPreview(
        "Create a fresh container for each omitted or None argument, as documented. Explicit containers are preserved.",
        path,
        validated,
        finding.span,
    )
