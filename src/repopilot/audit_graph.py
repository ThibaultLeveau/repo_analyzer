import operator
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from repopilot import specialists
from repopilot.audit import Audit, Finding, analyze_audit
from repopilot.dependencies import DependencyReport
from repopilot.events import measure
from repopilot.maintenance import maintenance_findings
from repopilot.python_facts import PythonFacts
from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class Outcome:
    name: str
    findings: tuple[Finding, ...] = ()
    failed: bool = False


class AuditState(TypedDict):
    snapshot: SourceSnapshot
    facts: PythonFacts
    report: Audit
    outcomes: Annotated[list[Outcome], operator.add]


def combine(base: Audit, outcomes: Iterable[Outcome]) -> Audit:
    findings = list(base.findings)
    failed = []
    for outcome in outcomes:
        if outcome.failed:
            failed.append(outcome.name)
        else:
            findings.extend(outcome.findings)

    def identity(f: Finding) -> tuple[str, ...]:
        return f.category, f.rule, f.title, f.severity, f.span.path

    merged: list[Finding] = []
    for item in sorted(
        findings,
        key=lambda f: (identity(f), f.span.start, f.span.end, f.explanation, f.limit),
    ):
        if (
            merged
            and identity(merged[-1]) == identity(item)
            and item.span.start <= merged[-1].span.end
        ):
            previous = merged[-1]
            merged[-1] = replace(
                previous,
                span=replace(previous.span, end=max(previous.span.end, item.span.end)),
            )
        else:
            merged.append(item)
    merged.sort(
        key=lambda f: (
            f.severity != "warning",
            f.span.path,
            f.span.start,
            f.rule,
            f.title,
        )
    )
    return replace(
        base, findings=tuple(merged), failed_specialists=tuple(sorted(failed))
    )


@dataclass(frozen=True)
class SpecialistNode:
    name: str
    check: Callable[[AuditState], list[Finding]]

    def __call__(self, state: AuditState) -> dict[str, list[Outcome]]:
        try:
            with measure("audit." + self.name):
                outcome = Outcome(self.name, tuple(self.check(state)))
        except Exception:  # noqa: BLE001 -- Isolate each specialist and omit source data from diagnostics.
            outcome = Outcome(self.name, failed=True)
        return {"outcomes": [outcome]}


def _merge(state: AuditState) -> dict[str, Audit]:
    return {"report": combine(state["report"], state["outcomes"])}


def _graph() -> CompiledStateGraph[AuditState, None, AuditState, AuditState]:
    graph = StateGraph(AuditState)
    nodes = [
        SpecialistNode(
            "security",
            lambda s: specialists.security(
                specialists.parsed_modules(s["snapshot"], s["facts"])
            ),
        ),
        SpecialistNode(
            "performance",
            lambda s: specialists.performance(
                specialists.parsed_modules(s["snapshot"], s["facts"])
            ),
        ),
        SpecialistNode("style", lambda s: specialists.style(s["facts"])),
    ]
    for node in nodes:
        graph.add_node(node.name, node)
        graph.add_edge(START, node.name)
    graph.add_node("combine", _merge)
    graph.add_edge([node.name for node in nodes], "combine")
    graph.add_edge("combine", END)
    return graph.compile()


_AUDIT = _graph()


def unified_audit(
    snapshot: SourceSnapshot, facts: PythonFacts, deps: DependencyReport
) -> Audit:
    base = analyze_audit(snapshot, facts, deps, include_style=False)
    extra = tuple(
        f for module in facts.modules for f in maintenance_findings(snapshot, module)
    )
    base = replace(base, findings=base.findings + extra)
    result = _AUDIT.invoke(
        {"snapshot": snapshot, "facts": facts, "report": base, "outcomes": []},
        config={"max_concurrency": 3},
    )
    report: Audit = result["report"]
    return report
