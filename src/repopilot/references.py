import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from repopilot.grounding import Evidence
from repopilot.python_facts import Module, PythonFacts, Span, Symbol
from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class Reference:
    caller: str
    text: str
    span: Span
    kind: Literal["call", "import"]
    target: Symbol | Module | None
    test: bool


@dataclass(frozen=True)
class SymbolDetail:
    symbol: Symbol
    incoming: list[Reference]
    outgoing: list[Reference]
    tests: list[Reference]

    def evidence(self, snapshot: SourceSnapshot, repo: str, sha: str) -> list[Evidence]:
        spans = [self.symbol.span] + [
            r.span
            for group in (self.incoming, self.outgoing, self.tests)
            for r in group[:3]
        ]
        result = []
        for span in dict.fromkeys(spans):
            end = min(span.end, span.start + 15)
            excerpt = "\n".join(snapshot.lines(span.path)[span.start - 1 : end])
            result.append(
                Evidence(repo, sha, Span(span.path, span.start, end), excerpt[:2000])
            )
        return result


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (root := _dotted(node.value)):
        return f"{root}.{node.attr}"
    return None


class _Scanner(ast.NodeVisitor):
    def __init__(
        self,
        module: Module,
        definitions: dict[str, Symbol],
        modules: dict[str, Module],
        namespaces: set[str],
    ) -> None:
        self.module = module
        self.definitions = definitions
        self.modules = modules
        self.namespaces = namespaces
        self.scopes = [module.name]
        self.owner = module.name
        self.class_body = False
        self.attribute_writes: set[str] = set()
        self.bindings: dict[tuple[str, str], list[str | None]] = defaultdict(list)
        self.imports = iter(i for i in module.imports if not i.dynamic)
        self.calls: list[tuple[str, str | None, tuple[str, ...], Span]] = []
        self.refs: list[Reference] = []
        path = PurePosixPath(module.span.path)
        self.test = (
            bool({"test", "tests"} & set(path.parts[:-1]))
            or path.name.startswith("test_")
            or path.name.endswith("_test.py")
        )

    def _define(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    ) -> None:
        original, old_scopes, was_class = self.owner, self.scopes, self.class_body
        name = f"{original}.{node.name}"
        self.bindings[original, node.name].append(name)
        for expression in node.decorator_list:
            self.visit(expression)
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                self.visit(base)
            for keyword in node.keywords:
                self.visit(keyword.value)
        else:
            for default in (*node.args.defaults, *node.args.kw_defaults):
                if default is not None:
                    self.visit(default)
        self.scopes = [name, *(old_scopes[1:] if was_class else old_scopes)]
        self.owner = name
        self.class_body = isinstance(node, ast.ClassDef)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in ast.iter_child_nodes(node.args):
                if isinstance(arg, ast.arg):
                    self.bindings[name, arg.arg].append(None)
        for stmt in node.body:
            self.visit(stmt)
        self.owner, self.scopes, self.class_body = original, old_scopes, was_class

    visit_FunctionDef = _define
    visit_AsyncFunctionDef = _define
    visit_ClassDef = _define

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bindings[self.owner, node.id].append(None)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and (name := _dotted(node)):
            self.attribute_writes.add(name)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self.bindings[self.owner, node.name].append(None)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self.bindings[self.owner, node.rest].append(None)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.bindings[self.owner, node.name].append(None)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self.bindings[self.owner, node.name].append(None)
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global | ast.Nonlocal) -> None:
        for name in node.names:
            for scope in self.scopes:
                self.bindings[scope, name].append(None)

    visit_Nonlocal = visit_Global

    def _anonymous(self, node: ast.AST) -> None:
        # Anonymous scopes can introduce new bindings. Their dispatch is unresolved.
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                self._call(child, None)

    visit_Lambda = _anonymous
    visit_ListComp = _anonymous
    visit_SetComp = _anonymous
    visit_DictComp = _anonymous
    visit_GeneratorExp = _anonymous

    def visit_Import(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            fact = next(self.imports)
            target = fact.target if fact.kind == "local" else None
            binding = alias.asname or alias.name.split(".")[0]
            if isinstance(node, ast.ImportFrom) and target and fact.name:
                if not target.endswith("." + fact.name) or target == fact.module:
                    target = f"{target}.{fact.name}"
            elif isinstance(node, ast.Import) and not alias.asname and target:
                target = binding
            self.bindings[self.owner, binding].append(target)
            imported = (
                (
                    self.modules.get(fact.target or "")
                    or self.definitions.get(target or "")
                )
                if isinstance(node, ast.Import)
                else (
                    self.definitions.get(target or "") or self.modules.get(target or "")
                )
            )
            self.refs.append(
                Reference(
                    self.owner, alias.name, fact.span, "import", imported, self.test
                )
            )

    visit_ImportFrom = visit_Import

    def _call(self, node: ast.Call, dotted: str | None) -> None:
        self.calls.append(
            (
                self.owner,
                dotted,
                tuple(self.scopes),
                Span(
                    self.module.span.path, node.lineno, node.end_lineno or node.lineno
                ),
            )
        )

    def visit_Call(self, node: ast.Call) -> None:
        self._call(node, _dotted(node.func))
        self.generic_visit(node)

    def resolve(self) -> list[Reference]:
        for owner, dotted, scopes, span in self.calls:
            target = None
            if dotted and dotted not in self.attribute_writes:
                first, _, rest = dotted.partition(".")
                for scope in scopes:
                    bindings = self.bindings.get((scope, first))
                    if (scope, "*") in self.bindings:
                        break
                    if bindings:
                        if len(bindings) == 1 and (name := bindings[0]):
                            full_name = f"{name}.{rest}" if rest else name
                            prefix = name
                            for segment in rest.split(".") if rest else ():
                                parent = self.definitions.get(prefix)
                                if prefix not in self.namespaces and (
                                    not parent or parent.kind != "class"
                                ):
                                    break
                                prefix += "." + segment
                            candidate = (
                                self.definitions.get(full_name)
                                if prefix == full_name
                                else None
                            )
                            if candidate and candidate.kind != "assignment":
                                target = candidate
                        break
            self.refs.append(
                Reference(
                    owner,
                    dotted or "Dynamic expression",
                    span,
                    "call",
                    target,
                    self.test,
                )
            )
        return self.refs


class ReferenceIndex:
    def __init__(self, snapshot: SourceSnapshot, facts: PythonFacts) -> None:
        counts = Counter(s.name for m in facts.modules for s in m.symbols)
        module_counts = Counter(m.name for m in facts.modules)
        modules = {m.name: m for m in facts.modules if module_counts[m.name] == 1}
        namespaces = set(modules) | set(facts.packages)
        self.definitions = {
            s.name: s
            for m in modules.values()
            for s in m.symbols
            if counts[s.name] == 1
        }
        self.refs: list[Reference] = []
        for module in facts.modules:
            scanner = _Scanner(module, self.definitions, modules, namespaces)
            scanner.visit(ast.parse(snapshot.read_text(module.span.path)))
            self.refs.extend(scanner.resolve())
        self.refs.sort(
            key=lambda r: (r.span.path, r.span.start, r.span.end, r.kind, r.text)
        )

    def detail(self, symbol: Symbol) -> SymbolDetail:
        uses = [r for r in self.refs if r.target == symbol]
        outgoing = [
            r
            for r in self.refs
            if r.caller == symbol.name
            and r.span.path == symbol.span.path
            and symbol.span.start <= r.span.start <= symbol.span.end
        ]
        return SymbolDetail(
            symbol,
            [r for r in uses if not r.test],
            outgoing,
            [r for r in uses if r.test],
        )
