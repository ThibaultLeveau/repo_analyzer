import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import replace

from repopilot.audit import Finding, style_findings
from repopilot.grounding import Evidence, StructuredModel, grounded_answer
from repopilot.python_facts import Module, PythonFacts, Span
from repopilot.snapshots import SourceSnapshot

_SENSITIVE = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "credential",
}
_LEVELS = {"debug", "info", "warning", "error", "critical", "exception", "log"}


def parsed_modules(
    snapshot: SourceSnapshot, facts: PythonFacts
) -> Iterator[tuple[Module, ast.Module]]:
    for module in facts.modules:
        if module.span.path.endswith(".py"):
            yield module, ast.parse(snapshot.read_text(module.span.path))


def _site(module: Module, node: ast.expr) -> Span:
    return Span(module.span.path, node.lineno, node.end_lineno or node.lineno)


def _identifier(value: ast.AST) -> str:
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return ""


def _sensitive(value: ast.AST) -> bool:
    if isinstance(value, ast.JoinedStr):
        return any(
            isinstance(v, ast.FormattedValue) and _sensitive(v.value)
            for v in value.values
        )
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", _identifier(value)).lower()
    return any(name == word or name.endswith("_" + word) for word in _SENSITIVE)


def _logger_factory(expr: ast.expr, logging: set[str], getters: set[str]) -> bool:
    return (
        (
            isinstance(expr, ast.Attribute)
            and expr.attr == "getLogger"
            and isinstance(expr.value, ast.Name)
            and expr.value.id in logging
        )
        or isinstance(expr, ast.Name)
        and expr.id in getters
    )


def security(parsed: Iterable[tuple[Module, ast.Module]]) -> list[Finding]:
    findings = []
    for module, tree in parsed:
        logging = {
            a.asname or "logging"
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for a in node.names
            if a.name == "logging"
        }
        getters = {
            a.asname or a.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            and node.module == "logging"
            and not node.level
            for a in node.names
            if a.name == "getLogger"
        }

        receivers = set(logging)
        for item in ast.walk(tree):
            if (
                isinstance(item, ast.Assign)
                and isinstance(item.value, ast.Call)
                and _logger_factory(item.value.func, logging, getters)
            ):
                receivers.update(t.id for t in item.targets if isinstance(t, ast.Name))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(
                node.func, ast.Attribute
            ):
                continue
            receiver = node.func.value
            if (
                node.func.attr not in _LEVELS
                or not isinstance(receiver, ast.Name)
                or receiver.id not in receivers
            ):
                continue
            if any(_sensitive(arg) for arg in node.args):
                findings.append(
                    Finding(
                        "SEC001",
                        "warning",
                        "Potentially sensitive value passed to logging",
                        "A logging call receives a value whose identifier suggests a credential.",
                        "Names and logger bindings are static clues. The runtime value and log destination are unknown.",
                        _site(module, node),
                        category="security",
                    )
                )
    return findings


def _loop_calls(body: list[ast.stmt]) -> Iterator[ast.Call]:
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        node = stack.pop()
        if isinstance(
            node,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
            ),
        ):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def performance(parsed: Iterable[tuple[Module, ast.Module]]) -> list[Finding]:
    findings = []
    for module, tree in parsed:
        aliases = {
            a.asname or "re"
            for stmt in tree.body
            if isinstance(stmt, ast.Import)
            for a in stmt.names
            if a.name == "re"
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                continue
            for call in _loop_calls(node.body):
                func = call.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == "compile"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in aliases
                    and len(call.args) == 1
                    and not call.keywords
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, (str, bytes))
                ):
                    findings.append(
                        Finding(
                            "PERF001",
                            "info",
                            "Constant regular expression compiled inside a loop",
                            "The loop body calls re.compile with a literal pattern.",
                            "Execution counts and runtime cost are unknown. Python can cache compiled patterns.",
                            _site(module, call),
                            category="performance",
                        )
                    )
    return findings


def style(facts: PythonFacts) -> list[Finding]:
    return [
        item
        for module in facts.modules
        if module.span.path.endswith(".py")
        for item in style_findings(module)
    ]


def explain(
    findings: list[Finding], repo: str, sha: str, model: StructuredModel | None
) -> list[Finding]:
    if not findings:
        return findings
    sources = [
        Evidence(repo, sha, item.span, item.explanation + " " + item.limit)
        for item in findings[:6]
    ]
    if len({source.id for source in sources}) != len(sources):
        return findings
    answer = grounded_answer(
        "Briefly explain each observed static heuristic and its uncertainty. "
        "Use one source per claim. Do not infer runtime behavior.",
        sources,
        model=model,
    )
    notes = {
        claim.sources[0].id: claim.text
        for claim in answer.claims
        if len(claim.sources) == 1
    }
    return [
        replace(item, model_note=notes.get(source.id, ""))
        for item, source in zip(findings[: len(sources)], sources, strict=True)
    ] + findings[len(sources) :]
