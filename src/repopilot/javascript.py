import posixpath
import re
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Literal

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from repopilot.manifests import npm_declarations
from repopilot.python_facts import ImportFact, Module, Span, Symbol
from repopilot.python_facts import Parser as ModuleParser
from repopilot.snapshots import SourceSnapshot

SUFFIXES = (".js", ".jsx", ".ts", ".tsx")
_FUNCTIONS = {
    "function_declaration",
    "function_expression",
    "generator_function",
    "generator_function_declaration",
    "arrow_function",
}
_CLASSES = {"class", "class_declaration", "abstract_class_declaration"}
_LANGUAGES = {
    ".js": Language(tree_sitter_javascript.language()),
    ".ts": Language(tree_sitter_typescript.language_typescript()),
    ".tsx": Language(tree_sitter_typescript.language_tsx()),
}
_BARE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(?:/[\w./-]+)?")


def package_name(specifier: str) -> str:
    parts = specifier.split("/")
    return "/".join(parts[:2]) if specifier.startswith("@") else parts[0]


def _text(node: Node | None) -> str:
    return (node.text or b"").decode() if node else ""


def _span(path: str, node: Node) -> Span:
    return Span(
        path,
        node.start_point.row + 1,
        max(
            node.start_point.row + 1,
            node.end_point.row + int(node.end_point.column > 0),
        ),
    )


def _bindings(node: Node) -> Iterator[tuple[str | None, str | None]]:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in {"import_specifier", "export_specifier"}:
            yield (
                _text(current.child_by_field_name("name")),
                _text(current.child_by_field_name("alias")) or None,
            )
        elif current.type == "namespace_import":
            yield "*", _text(current.named_children[-1])
        elif (
            current.type == "identifier"
            and current.parent
            and current.parent.type == "import_clause"
        ):
            yield "default", _text(current)
        elif current.type in {
            "import_statement",
            "export_statement",
            "import_clause",
            "named_imports",
            "export_clause",
        }:
            stack.extend(reversed(current.named_children))


def _resolve(
    path: str, spec: str, files: set[str], declared: set[str]
) -> tuple[Literal["local", "stdlib", "unresolved", "external"], str | None]:
    if spec.startswith(("./", "../")):
        base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
        candidates = (
            {base}
            if base in files
            else {base + ext for ext in SUFFIXES}
            | {base + "/index" + ext for ext in SUFFIXES}
        )
        if base.endswith(".js") and base not in files:
            candidates.update(base[:-3] + ext for ext in (".ts", ".tsx", ".jsx"))
        matches = candidates.intersection(files)
        return (
            ("local", next(iter(matches)))
            if len(matches) == 1
            else ("unresolved", None)
        )
    if spec.startswith("node:"):
        return "stdlib", spec
    if _BARE.fullmatch(spec) and package_name(spec) in declared:
        return "external", spec
    return "unresolved", None


class JavaScriptAnalyzer:
    suffixes = SUFFIXES

    def prepare(self, snapshot: SourceSnapshot, paths: tuple[str, ...]) -> ModuleParser:
        files = set(paths)
        manifests: dict[PurePosixPath, set[str]] = {}
        for entry in snapshot.tree():
            location = PurePosixPath(entry.path)
            if location.name != "package.json" or entry.kind == "directory":
                continue
            names: set[str] = set()
            if entry.kind == "text":
                declarations, _ = npm_declarations(
                    entry.path, snapshot.read_text(entry.path)
                )
                names.update(d.name.removeprefix("npm:") for d in declarations)
            manifests[location.parent] = names
        parsers = {ext: Parser(lang) for ext, lang in _LANGUAGES.items()}

        def parse(path: str, source: str) -> Module:
            ext = PurePosixPath(path).suffix
            declared = next(
                (
                    manifests[parent]
                    for parent in PurePosixPath(path).parents
                    if parent in manifests
                ),
                set(),
            )
            tree = (
                parsers[".js" if ext == ".jsx" else ext]
                .parse(source.encode())
                .root_node
            )
            if tree.has_error:
                node = tree
                while child := next((n for n in node.children if n.has_error), None):
                    node = child
                raise SyntaxError(
                    "Invalid JavaScript or TypeScript syntax",
                    (path, node.start_point.row + 1, node.start_point.column + 1, ""),
                )
            symbols: list[Symbol] = []
            imports: list[ImportFact] = []
            stack = [(tree, path)]
            while stack:
                node, scope = stack.pop()
                kind = node.type
                label = _text(node.child_by_field_name("name"))
                parent = node.parent
                if not label and parent and parent.type == "variable_declarator":
                    ident = parent.child_by_field_name("name")
                    if ident and ident.type == "identifier":
                        label = _text(ident)
                if (
                    not label
                    and parent
                    and parent.type == "export_statement"
                    and any(c.type == "default" for c in parent.children)
                ):
                    label = "default"
                category: Literal["class", "method", "function"] | None = None
                if kind in _CLASSES:
                    category = "class"
                elif kind == "method_definition":
                    category = "method"
                elif kind in _FUNCTIONS:
                    category = "function"
                if label and category:
                    scope = f"{scope}.{label}"
                    symbols.append(
                        Symbol(
                            scope,
                            category,
                            _span(path, node),
                            any(c.type == "async" for c in node.children),
                        )
                    )
                callee = node.child_by_field_name("function")
                dynamic = (
                    kind == "call_expression"
                    and callee is not None
                    and callee.type == "import"
                )
                if kind in {"import_statement", "export_statement"} or dynamic:
                    literal = node.child_by_field_name("source")
                    if dynamic:
                        args = node.child_by_field_name("arguments")
                        literal = (
                            args.named_children[0]
                            if args and args.named_children
                            else None
                        )
                    if literal or dynamic:
                        spec = (
                            _text(literal)[1:-1]
                            if literal
                            and literal.type == "string"
                            and not any(
                                c.type == "escape_sequence"
                                for c in literal.named_children
                            )
                            else ""
                        )
                        origin, target = _resolve(path, spec, files, declared)
                        bindings = list(_bindings(node)) or [(None, None)]
                        imports.extend(
                            ImportFact(
                                scope,
                                spec or "dynamic import",
                                name,
                                alias,
                                target,
                                origin,
                                _span(path, node),
                                dynamic,
                            )
                            for name, alias in bindings
                        )
                stack.extend((child, scope) for child in reversed(node.named_children))
            return Module(
                path, False, _span(path, tree), None, tuple(symbols), tuple(imports)
            )

        return ModuleParser(parse)
