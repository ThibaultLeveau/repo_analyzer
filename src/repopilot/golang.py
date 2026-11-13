import json
import posixpath
from pathlib import PurePosixPath
from typing import Literal

import tree_sitter_go
from tree_sitter import Language, Node, Parser

from repopilot.manifests import go_manifest
from repopilot.python_facts import ImportFact, Module, Span, Symbol
from repopilot.python_facts import Parser as ModuleParser
from repopilot.snapshots import SourceSnapshot


def _text(node: Node | None) -> str:
    return (node.text or b"").decode() if node else ""


def _receiver(node: Node) -> str:
    params = node.child_by_field_name("receiver")
    if not params or not params.named_children:
        return ""
    kind = params.named_children[0].child_by_field_name("type")
    while kind and kind.type in {"pointer_type", "generic_type", "parenthesized_type"}:
        kind = kind.child_by_field_name("type") or (
            kind.named_children[0] if kind.named_children else None
        )
    return _text(kind) if kind and kind.type == "type_identifier" else ""


class GoAnalyzer:
    suffixes = (".go",)

    def prepare(self, snapshot: SourceSnapshot, paths: tuple[str, ...]) -> ModuleParser:
        roots: dict[PurePosixPath, tuple[str, tuple[str, ...]]] = {}
        for entry in snapshot.tree():
            location = PurePosixPath(entry.path)
            if location.name == "go.mod" and entry.kind != "directory":
                module, decls, _ = (
                    go_manifest(entry.path, snapshot.read_text(entry.path))
                    if entry.kind == "text"
                    else ("", [], [])
                )
                roots[location.parent] = (
                    module,
                    tuple(sorted((d.name[3:] for d in decls), key=len, reverse=True)),
                )
        context: dict[str, tuple[str, tuple[str, ...], str]] = {}
        package_paths: set[str] = set()
        for path in paths:
            directory = PurePosixPath(path).parent
            root = next(
                (p for p in (directory, *directory.parents) if p in roots), None
            )
            module, external = roots[root] if root is not None else ("", ())
            rel = (
                str(directory.relative_to(root)) if root is not None else str(directory)
            )
            imported = posixpath.normpath(posixpath.join(module, rel)) if module else ""
            if imported:
                package_paths.add(imported)
            context[path] = module, external, imported
        parser = Parser(Language(tree_sitter_go.language()))

        def parse(path: str, source: str) -> Module:
            root = parser.parse(source.encode()).root_node
            if root.has_error:
                error = root
                while child := next((c for c in error.children if c.has_error), None):
                    error = child
                raise SyntaxError(
                    "Invalid Go syntax",
                    (path, error.start_point.row + 1, error.start_point.column + 1, ""),
                )
            clause = next(
                (c for c in root.named_children if c.type == "package_clause"), None
            )
            if not clause or not clause.named_children:
                raise ValueError("Missing Go package declaration")
            package = _text(clause.named_children[-1])
            module, external, imported = context[path]
            identity = imported or str(PurePosixPath(path).parent)
            if identity == ".":
                identity = package
            if path.endswith("_test.go") and package.endswith("_test"):
                identity += "_test"

            def span(node: Node) -> Span:
                return Span(
                    path,
                    node.start_point.row + 1,
                    max(
                        node.start_point.row + 1,
                        node.end_point.row + int(node.end_point.column > 0),
                    ),
                )

            symbols = [Symbol(identity + "." + package, "package", span(clause))]
            imports: list[ImportFact] = []
            for node in root.named_children:
                name = _text(node.child_by_field_name("name"))
                if node.type == "function_declaration":
                    symbols.append(
                        Symbol(identity + "." + name, "function", span(node))
                    )
                elif node.type == "method_declaration":
                    receiver = _receiver(node)
                    if receiver:
                        symbols.append(
                            Symbol(
                                f"{identity}.{receiver}.{name}", "method", span(node)
                            )
                        )
                elif node.type == "type_declaration":
                    for spec in node.named_children:
                        if typename := spec.child_by_field_name("name"):
                            symbols.append(
                                Symbol(
                                    identity + "." + _text(typename), "type", span(spec)
                                )
                            )
                elif node.type == "import_declaration":
                    stack = list(reversed(node.named_children))
                    while stack:
                        spec = stack.pop()
                        if spec.type == "import_spec_list":
                            stack.extend(reversed(spec.named_children))
                            continue
                        if spec.type != "import_spec":
                            continue
                        literal = _text(spec.child_by_field_name("path"))
                        try:
                            target = (
                                literal[1:-1]
                                if literal.startswith("`")
                                else json.loads(literal)
                            )
                        except ValueError:
                            target = ""
                        alias = _text(spec.child_by_field_name("name")) or None
                        kind: Literal["stdlib", "external", "local", "unresolved"] = (
                            "unresolved"
                        )
                        provider: str | None = None
                        if (
                            target in package_paths
                            and module
                            and (target == module or target.startswith(module + "/"))
                        ):
                            kind, provider = "local", target
                        elif dep := next(
                            (
                                d
                                for d in external
                                if target == d or target.startswith(d + "/")
                            ),
                            None,
                        ):
                            kind, provider = "external", dep
                        elif (
                            target
                            and not target.startswith((".", "/"))
                            and "." not in target.split("/")[0]
                        ):
                            kind, provider = "stdlib", target
                        imports.append(
                            ImportFact(
                                identity,
                                target,
                                None,
                                alias,
                                provider,
                                kind,
                                span(spec),
                            )
                        )
            return Module(
                identity, False, span(root), None, tuple(symbols), tuple(imports)
            )

        return ModuleParser(parse, tuple(sorted(package_paths)))
