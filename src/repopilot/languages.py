from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import Protocol

from repopilot.javascript import JavaScriptAnalyzer
from repopilot.python_facts import (
    Diagnostic,
    Module,
    Parser,
    PythonAnalyzer,
    PythonFacts,
)
from repopilot.snapshots import Entry, SourceSnapshot


class Analyzer(Protocol):
    @property
    def suffixes(self) -> tuple[str, ...]: ...

    def prepare(self, snapshot: SourceSnapshot, paths: tuple[str, ...]) -> Parser: ...


def analyze(snapshot: SourceSnapshot, analyzers: Sequence[Analyzer]) -> PythonFacts:
    kinds: dict[str, int] = {}
    groups: list[list[Entry]] = [[] for _ in analyzers]
    for index, analyzer in enumerate(analyzers):
        for suffix in analyzer.suffixes:
            if suffix in kinds:
                raise ValueError(f"Multiple built-in analyzers for {suffix}")
            kinds[suffix] = index
    for entry in snapshot.tree():
        kind = kinds.get(PurePosixPath(entry.path).suffix)
        if kind is not None and entry.kind != "directory":
            groups[kind].append(entry)

    modules: list[Module] = []
    packages: set[str] = set()
    diagnostics: list[Diagnostic] = []
    for analyzer, group in zip(analyzers, groups, strict=True):
        if not group:
            continue
        try:
            parser = analyzer.prepare(snapshot, tuple(entry.path for entry in group))
        except (ValueError, RuntimeError, SyntaxError) as exc:
            diagnostics.extend(
                Diagnostic(entry.path, None, str(exc)) for entry in group
            )
            continue
        packages.update(parser.packages)
        for entry in group:
            path = entry.path
            if entry.kind != "text":
                diagnostics.append(
                    Diagnostic(path, None, f"Cannot parse {entry.kind} entry")
                )
                continue
            text = snapshot.read_text(path)
            try:
                modules.append(parser.parse(path, text))
            except SyntaxError as exc:
                diagnostics.append(Diagnostic(path, exc.lineno, exc.msg))
            except (ValueError, RuntimeError) as exc:
                diagnostics.append(Diagnostic(path, None, str(exc)))
    return PythonFacts(
        tuple(sorted(modules, key=lambda m: m.span.path)),
        tuple(sorted(packages)),
        tuple(sorted(diagnostics, key=lambda d: d.path)),
    )


BUILTINS: tuple[Analyzer, ...] = (PythonAnalyzer(), JavaScriptAnalyzer())


def analyze_snapshot(snapshot: SourceSnapshot) -> PythonFacts:
    return analyze(snapshot, BUILTINS)


def supported(path: str) -> bool:
    suffix = PurePosixPath(path).suffix
    return any(suffix in analyzer.suffixes for analyzer in BUILTINS)
