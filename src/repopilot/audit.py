import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from repopilot.dependencies import DependencyReport
from repopilot.python_facts import PythonFacts, Span
from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Literal["info", "warning"]
    title: str
    explanation: str
    limit: str
    span: Span
    distribution: str | None = None

    @property
    def key(self) -> str:
        return f"{self.rule}/{self.span.path}/{self.span.start}/{self.title}"


@dataclass(frozen=True)
class Audit:
    findings: tuple[Finding, ...]
    test_files: int
    skipped_modules: int
    coverage: tuple[int, int] | None


def _test_name(path: PurePosixPath) -> bool:
    return path.suffix == ".py" and (
        path.stem.startswith("test_") or path.stem.endswith("_test")
    )


def _coverage(snapshot: SourceSnapshot) -> tuple[int, int] | None:
    try:
        data = json.loads(snapshot.read_text("coverage.json"))
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    meta, totals = data.get("meta"), data.get("totals")
    if (
        not isinstance(meta, dict)
        or meta.get("format") != 3
        or not isinstance(totals, dict)
    ):
        return None
    covered, total = totals.get("covered_lines"), totals.get("num_statements")
    if (
        type(covered) is int
        and type(total) is int
        and 0 <= covered <= total
        and total > 0
    ):
        return covered, total
    return None


def analyze_audit(
    snapshot: SourceSnapshot, facts: PythonFacts, deps: DependencyReport
) -> Audit:
    test_paths = {
        PurePosixPath(entry.path)
        for entry in snapshot.tree()
        if entry.kind in {"text", "oversized"} and _test_name(PurePosixPath(entry.path))
    }
    test_names = {path.stem for path in test_paths}
    findings: list[Finding] = []
    for module in facts.modules:
        path = PurePosixPath(module.span.path)
        if (
            not module.is_package
            and module.symbols
            and path not in test_paths
            and path.name != "conftest.py"
            and not {"tests", "test"}.intersection(path.parts[:-1])
            and {f"test_{path.stem}", f"{path.stem}_test"}.isdisjoint(test_names)
        ):
            findings.append(
                Finding(
                    "TEST001",
                    "info",
                    f"No matching test filename for {path.name}",
                    f"The snapshot contains neither test_{path.stem}.py nor {path.stem}_test.py.",
                    "Tests with other names or integration tests can exercise this module.",
                    module.symbols[0].span,
                )
            )
        for symbol in module.symbols:
            parent, _, name = symbol.name.rpartition(".")
            if (
                symbol.kind == "function"
                and parent == module.name
                and name.isascii()
                and any(c.isupper() for c in name)
            ):
                findings.append(
                    Finding(
                        "STYLE001",
                        "info",
                        f"Function name {name}",
                        "This module-level function name contains an uppercase letter.",
                        "An external API or compatibility requirement may determine the spelling.",
                        symbol.span,
                    )
                )
    if deps.manifests.paths and not deps.manifests.diagnostics:
        for package in deps.packages.values():
            if package.observation == "Possibly undeclared":
                use = next(u for u in package.uses if not u.candidates)
                findings.append(
                    Finding(
                        "DEP001",
                        "warning",
                        f"Possibly undeclared dependency {package.name}",
                        f"An import maps to {package.name}, which is absent from the parsed declarations.",
                        "Distribution names can differ from import names. Runtime configuration may supply it.",
                        use.fact.span,
                        package.name,
                    )
                )
    findings.sort(
        key=lambda f: (
            f.severity != "warning",
            f.span.path,
            f.span.start,
            f.rule,
            f.title,
        )
    )
    return Audit(
        tuple(findings), len(test_paths), len(facts.diagnostics), _coverage(snapshot)
    )
