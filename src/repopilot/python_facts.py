import ast
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from importlib.util import resolve_name
from pathlib import PurePosixPath
from typing import Literal

from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class Span:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Symbol:
    name: str
    kind: Literal["class", "method", "function", "assignment", "type", "package"]
    span: Span
    is_async: bool = False


@dataclass(frozen=True)
class ImportFact:
    scope: str
    module: str
    name: str | None
    alias: str | None
    target: str | None
    kind: Literal["local", "stdlib", "external", "unresolved"]
    span: Span
    dynamic: bool = False


@dataclass(frozen=True)
class Module:
    name: str
    is_package: bool
    span: Span
    docstring: str | None
    symbols: tuple[Symbol, ...]
    imports: tuple[ImportFact, ...]
    doc_span: Span | None = None


@dataclass(frozen=True)
class Diagnostic:
    path: str
    line: int | None
    message: str


@dataclass(frozen=True)
class PythonFacts:
    modules: tuple[Module, ...]
    packages: tuple[str, ...]
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True)
class Parser:
    parse: Callable[[str, str], Module]
    packages: tuple[str, ...] = ()


def _module_name(path: str) -> tuple[str, bool]:
    parts = list(PurePosixPath(path).with_suffix("").parts)
    if len(parts) > 1 and parts[0] == "src":
        parts.pop(0)
    package = parts[-1] == "__init__"
    if package and len(parts) > 1:
        parts.pop()
    return ".".join(parts), package


def _targets(node: ast.expr) -> Iterator[str]:
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        yield f"{node.value.id}.{node.attr}"
    elif isinstance(node, (ast.List, ast.Tuple)):
        for child in node.elts:
            yield from _targets(child)
    elif isinstance(node, ast.Starred):
        yield from _targets(node.value)


class _Visitor(ast.NodeVisitor):
    def __init__(
        self, path: str, module: str, package: str, known: Counter[str]
    ) -> None:
        self.path = path
        self.scope = module
        self.in_class = False
        self.package = package
        self.known = known
        self.symbols: list[Symbol] = []
        self.imports: list[ImportFact] = []

    def _span(self, node: ast.stmt) -> Span:
        start = node.lineno
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.decorator_list
        ):
            start = node.decorator_list[0].lineno
        return Span(self.path, start, node.end_lineno or node.lineno)

    def _definition(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        parent, was_class = self.scope, self.in_class
        self.scope = f"{parent}.{node.name}"
        self.in_class = isinstance(node, ast.ClassDef)
        kind: Literal["class", "method", "function"] = (
            "class" if self.in_class else "method" if was_class else "function"
        )
        self.symbols.append(
            Symbol(
                self.scope,
                kind,
                self._span(node),
                isinstance(node, ast.AsyncFunctionDef),
            )
        )
        self.generic_visit(node)
        self.scope, self.in_class = parent, was_class

    visit_ClassDef = _definition
    visit_FunctionDef = _definition
    visit_AsyncFunctionDef = _definition

    def _assignment(self, target: ast.expr, node: ast.stmt) -> None:
        for name in _targets(target):
            self.symbols.append(
                Symbol(f"{self.scope}.{name}", "assignment", self._span(node))
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._assignment(target, node)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assignment(node.target, node)
        if node.value:
            self.visit(node.value)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._assignment(node.target, node)
        self.visit(node.value)

    def _import(self, node: ast.Import | ast.ImportFrom, item: ast.alias) -> ImportFact:
        name = None
        module = item.name
        if isinstance(node, ast.ImportFrom):
            name = item.name
            module = "." * node.level + (node.module or "")
        target: str | None = module
        if module.startswith("."):
            try:
                target = resolve_name(module, self.package)
            except ImportError:
                target = None
        if name and target and f"{target}.{name}" in self.known:
            target = f"{target}.{name}"
        kind: Literal["local", "stdlib", "external", "unresolved"]
        if not target or self.known[target] > 1:
            kind = "unresolved"
        elif target in self.known:
            kind = "local"
        elif module.startswith(".") or target.split(".")[0] in self.known:
            kind = "unresolved"
        elif target.split(".")[0] in sys.stdlib_module_names:
            kind = "stdlib"
        else:
            kind = "external"
        return ImportFact(
            self.scope, module, name, item.asname, target, kind, self._span(node)
        )

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.extend(self._import(node, item) for item in node.names)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.extend(self._import(node, item) for item in node.names)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        literal_import = (isinstance(func, ast.Name) and func.id == "__import__") or (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        )
        if literal_import and node.args and isinstance(node.args[0], ast.Constant):
            name = node.args[0].value
            if isinstance(name, str) and all(
                part.isidentifier() for part in name.split(".")
            ):
                root = name.split(".")[0]
                if root not in self.known and root not in sys.stdlib_module_names:
                    self.imports.append(
                        ImportFact(
                            self.scope,
                            name,
                            None,
                            None,
                            name,
                            "external",
                            Span(
                                self.path, node.lineno, node.end_lineno or node.lineno
                            ),
                            dynamic=True,
                        )
                    )
        self.generic_visit(node)


class PythonAnalyzer:
    suffixes = (".py",)

    def prepare(self, snapshot: SourceSnapshot, paths: tuple[str, ...]) -> Parser:
        names = {path: _module_name(path) for path in paths}
        known = Counter(name for name, _ in names.values())
        packages: set[str] = set()
        for name, is_package in names.values():
            parts = name.split(".")
            for i in range(1, len(parts) + int(is_package)):
                packages.add(".".join(parts[:i]))
        for package in packages:
            known.setdefault(package, 1)

        def parse(path: str, text: str) -> Module:
            name, is_package = names[path]
            visitor = _Visitor(
                path, name, name if is_package else name.rpartition(".")[0], known
            )
            tree = ast.parse(text, filename=path)
            visitor.visit(tree)
            physical = text.replace("\r\n", "\n").replace("\r", "\n")
            last_line = physical.count("\n") + int(not physical.endswith("\n"))
            return Module(
                name,
                is_package,
                Span(path, 1, max(1, last_line)),
                ast.get_docstring(tree),
                tuple(visitor.symbols),
                tuple(visitor.imports),
                Span(
                    path,
                    tree.body[0].lineno,
                    tree.body[0].end_lineno or tree.body[0].lineno,
                )
                if ast.get_docstring(tree)
                else None,
            )

        return Parser(parse, tuple(sorted(packages)))


def analyze_python(snapshot: SourceSnapshot) -> PythonFacts:
    from repopilot.languages import analyze

    return analyze(snapshot, (PythonAnalyzer(),))
