import json
import posixpath
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

import tree_sitter_json
import tree_sitter_toml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from tree_sitter import Language, Node, Parser

from repopilot.python_facts import Diagnostic, Span
from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class Declaration:
    name: str
    constraint: str
    url: str | None
    extras: tuple[str, ...]
    marker: str | None
    group: str
    span: Span
    source: str


@dataclass(frozen=True)
class ManifestReport:
    paths: tuple[str, ...]
    declarations: tuple[Declaration, ...]
    diagnostics: tuple[Diagnostic, ...]


def _keys(node: Node, data: bytes) -> tuple[str, ...]:
    if node.type == "dotted_key":
        return tuple(key for child in node.named_children for key in _keys(child, data))
    text = data[node.start_byte : node.end_byte].decode()
    return (tomllib.loads("v=" + text)["v"] if node.type == "quoted_key" else text,)


def _pairs(
    node: Node, data: bytes, prefix: tuple[str, ...] = ()
) -> Iterator[tuple[tuple[str, ...], Node]]:
    for child in node.named_children:
        if child.type not in {"table", "pair"}:
            continue
        key, *values = (n for n in child.named_children if n.type != "comment")
        path = prefix + _keys(key, data)
        if child.type == "table":
            yield from _pairs(child, data, path)
        elif values[-1].type == "inline_table":
            yield from _pairs(values[-1], data, path)
        else:
            yield path, values[-1]


def _requirements_group(path: PurePosixPath) -> str:
    stem = path.stem.lower().replace("requirements", "").strip("-_.") or "runtime"
    return {"base": "runtime", "main": "runtime", "prod": "runtime"}.get(stem, stem)


def toml_pairs(data: bytes) -> Iterator[tuple[tuple[str, ...], Node]]:
    tomllib.loads(data.decode())
    tree = Parser(Language(tree_sitter_toml.language())).parse(data)
    if tree.root_node.has_error:
        raise ValueError("Unsupported TOML syntax for source locations")
    yield from _pairs(tree.root_node, data)


def _logical_lines(lines: list[str]) -> Iterator[tuple[int, int, str]]:
    parts: list[str] = []
    start = 1
    for index, line in enumerate(lines, 1):
        if not parts:
            start = index
        if line.rstrip().endswith("\\"):
            parts.append(line.rstrip()[:-1])
        else:
            yield start, index, "".join([*parts, line])
            parts.clear()
    if parts:
        yield start, len(lines), "".join(parts)


def go_manifest(
    path: str, text: str
) -> tuple[str, list[Declaration], list[Diagnostic]]:
    module = ""
    declarations: list[Declaration] = []
    diagnostics: list[Diagnostic] = []
    block = ""
    for number, source in enumerate(text.splitlines(), 1):
        line, _, comment = source.partition("//")
        parts = line.split()
        if not parts:
            continue
        if parts == [")"]:
            block = ""
            continue
        if len(parts) == 2 and parts[1] == "(":
            block = parts[0]
            continue
        directive, args = (block, parts) if block else (parts[0], parts[1:])
        if directive not in {"module", "require"}:
            continue
        if len(args) != (1 if directive == "module" else 2):
            diagnostics.append(Diagnostic(path, number, "Unsupported go.mod directive"))
            continue
        name = args[0]
        if name.startswith(('"', "`")) and name.endswith(name[0]):
            name = name[1:-1]
        if not name or any(c in name for c in ('"', "`", "\\")):
            diagnostics.append(Diagnostic(path, number, "Unsupported Go module path"))
            continue
        if directive == "module":
            module = name
        elif comment.strip().split()[:1] != ["indirect"]:
            declarations.append(
                Declaration(
                    "go:" + name,
                    args[1],
                    None,
                    (),
                    None,
                    "require",
                    Span(path, number, number),
                    source,
                )
            )
    return module, declarations, diagnostics


def npm_declarations(
    path: str, text: str
) -> tuple[list[Declaration], list[Diagnostic]]:
    declarations: list[Declaration] = []
    diagnostics: list[Diagnostic] = []
    raw = text.encode()
    tree = Parser(Language(tree_sitter_json.language())).parse(raw).root_node
    if (
        tree.has_error
        or not tree.named_children
        or tree.named_children[0].type != "object"
    ):
        diagnostics.append(Diagnostic(path, None, "Invalid package.json object"))
        return declarations, diagnostics
    groups = {
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    }
    for field in tree.named_children[0].named_children:
        key, value = (
            field.child_by_field_name("key"),
            field.child_by_field_name("value"),
        )
        if not key or not value:
            continue
        group = json.loads(raw[key.start_byte : key.end_byte])
        if group not in groups:
            continue
        if value.type != "object":
            diagnostics.append(
                Diagnostic(
                    path, value.start_point.row + 1, "Expected a dependency object"
                )
            )
            continue
        for pair in value.named_children:
            name, version = (
                pair.child_by_field_name("key"),
                pair.child_by_field_name("value"),
            )
            if not name or not version or version.type != "string":
                diagnostics.append(
                    Diagnostic(
                        path,
                        pair.start_point.row + 1,
                        "Expected a string dependency constraint",
                    )
                )
                continue
            package = json.loads(raw[name.start_byte : name.end_byte])
            if not re.fullmatch(
                r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*", package
            ):
                diagnostics.append(
                    Diagnostic(
                        path, pair.start_point.row + 1, "Unsupported npm package name"
                    )
                )
                continue
            declarations.append(
                Declaration(
                    "npm:" + package,
                    json.loads(raw[version.start_byte : version.end_byte]),
                    None,
                    (),
                    None,
                    group,
                    Span(path, pair.start_point.row + 1, pair.end_point.row + 1),
                    raw[pair.start_byte : pair.end_byte].decode(),
                )
            )
    return declarations, diagnostics


class _Reader:
    def __init__(self, files: dict[str, str], read_text: Callable[[str], str]) -> None:
        self.files = files
        self.read_text = read_text
        self.paths: set[str] = set()
        self.requirements_seen: set[tuple[str, str]] = set()
        self.declarations: list[Declaration] = []
        self.diagnostics: list[Diagnostic] = []

    def add(self, text: str, group: str, span: Span, source: str) -> None:
        try:
            req = Requirement(text)
        except InvalidRequirement:
            self.diagnostics.append(
                Diagnostic(span.path, span.start, "Unsupported dependency declaration")
            )
            return
        constraint = ""
        if req.specifier:
            tail = text.strip()[len(req.name) :].lstrip()
            if tail.startswith("["):
                tail = tail.partition("]")[2]
            constraint = tail.partition(";")[0].strip()
        self.declarations.append(
            Declaration(
                canonicalize_name(req.name),
                constraint,
                req.url,
                tuple(sorted(map(canonicalize_name, req.extras))),
                str(req.marker) if req.marker else None,
                group,
                span,
                source,
            )
        )

    def read(self, path: str) -> str | None:
        self.paths.add(path)
        if self.files.get(path) != "text":
            self.diagnostics.append(
                Diagnostic(path, None, "Manifest is missing or unreadable")
            )
            return None
        return self.read_text(path)

    def requirements(
        self, path: str, group: str, ancestors: tuple[str, ...] = ()
    ) -> None:
        if path in ancestors or len(ancestors) >= 16:
            self.diagnostics.append(
                Diagnostic(path, None, "Requirements include cycle or depth limit")
            )
            return
        if (path, group) in self.requirements_seen:
            return
        self.requirements_seen.add((path, group))
        text = self.read(path)
        if text is None:
            return
        lines = text.split("\n")
        for start, end, logical in _logical_lines(lines):
            value = re.sub(r"(?:^|\s+)#.*$", "", logical).strip()
            if not value:
                continue
            include = re.fullmatch(r"(?:-r\s*|--requirement(?:=|\s+))(.+)", value)
            if include:
                name = include[1].strip().strip("\"'")
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(path), name)
                )
                if (
                    ":" in name
                    or "\\" in name
                    or target.startswith(("/", "../"))
                    or target == ".."
                ):
                    self.diagnostics.append(
                        Diagnostic(
                            path, start, "Only repository-local includes are supported"
                        )
                    )
                else:
                    self.requirements(target, group, (*ancestors, path))
                continue
            value = re.sub(r"\s+--hash(?:=|\s+)\S+", "", value)
            self.add(
                value, group, Span(path, start, end), "\n".join(lines[start - 1 : end])
            )

    def pyproject(self, path: str) -> None:
        text = self.read(path)
        if text is None:
            return
        data = text.encode()
        lines = text.split("\n")
        try:
            pairs = list(toml_pairs(data))
        except ValueError as exc:
            self.diagnostics.append(Diagnostic(path, None, str(exc)))
            return
        groups: dict[str, Node] = {}
        for keys, value in pairs:
            if keys == ("project", "dependencies"):
                groups["runtime"] = value
            elif len(keys) == 3 and keys[:2] == ("project", "optional-dependencies"):
                groups["optional:" + keys[2]] = value
            elif len(keys) == 2 and keys[0] == "dependency-groups":
                groups["group:" + canonicalize_name(keys[1])] = value
            elif keys == ("project", "dynamic"):
                dynamic = tomllib.loads(
                    "v=" + data[value.start_byte : value.end_byte].decode()
                )["v"]
                if isinstance(dynamic, list) and any(
                    k in dynamic for k in ("dependencies", "optional-dependencies")
                ):
                    self.diagnostics.append(
                        Diagnostic(
                            path,
                            value.start_point.row + 1,
                            "Dynamic dependencies require build metadata",
                        )
                    )
            elif keys[:3] == ("tool", "poetry", "dependencies"):
                self.diagnostics.append(
                    Diagnostic(
                        path,
                        value.start_point.row + 1,
                        "Unsupported legacy Poetry dependency",
                    )
                )

        def read_group(
            key: str, output: str, visited: set[str], ancestors: tuple[str, ...]
        ) -> None:
            node = groups[key]
            if key in ancestors or len(ancestors) >= 16:
                self.diagnostics.append(
                    Diagnostic(
                        path,
                        node.start_point.row + 1,
                        "Dependency group cycle or depth limit",
                    )
                )
                return
            if key in visited:
                return
            visited.add(key)
            if node.type != "array":
                self.diagnostics.append(
                    Diagnostic(
                        path, node.start_point.row + 1, "Expected a dependency array"
                    )
                )
                return
            for item in node.named_children:
                if item.type == "comment":
                    continue
                start, end = item.start_point.row + 1, item.end_point.row + 1
                declared = tomllib.loads(
                    "v=" + data[item.start_byte : item.end_byte].decode()
                )["v"]
                if isinstance(declared, str):
                    self.add(
                        declared,
                        output,
                        Span(path, start, end),
                        "\n".join(lines[start - 1 : end]),
                    )
                elif (
                    key.startswith("group:")
                    and isinstance(declared, dict)
                    and isinstance(declared.get("include-group"), str)
                    and (
                        nested := "group:"
                        + canonicalize_name(declared["include-group"])
                    )
                    in groups
                ):
                    read_group(nested, output, visited, (*ancestors, key))
                else:
                    self.diagnostics.append(
                        Diagnostic(path, start, "Unsupported dependency group entry")
                    )

        for group in groups:
            read_group(group, group, set(), ())

    def npm(self, path: str) -> None:
        text = self.read(path)
        if text is not None:
            declarations, diagnostics = npm_declarations(path, text)
            self.declarations.extend(declarations)
            self.diagnostics.extend(diagnostics)

    def go(self, path: str) -> None:
        text = self.read(path)
        if text is not None:
            _, declarations, diagnostics = go_manifest(path, text)
            self.declarations.extend(declarations)
            self.diagnostics.extend(diagnostics)


def revision_declarations(path: str, text: str, group: str) -> tuple[Declaration, ...]:
    reader = _Reader({path: "text"}, lambda _: text)
    if PurePosixPath(path).name == "go.mod":
        reader.go(path)
    elif PurePosixPath(path).name == "package.json":
        reader.npm(path)
    elif PurePosixPath(path).name == "pyproject.toml":
        reader.pyproject(path)
    else:
        reader.requirements(path, group)
    if any(d.path == path for d in reader.diagnostics):
        raise ValueError("Unsupported manifest declaration in this revision")
    return tuple(reader.declarations)


def parse_manifests(snapshot: SourceSnapshot) -> ManifestReport:
    reader = _Reader(
        {e.path: e.kind for e in snapshot.tree() if e.kind != "directory"},
        snapshot.read_text,
    )
    for name in sorted(reader.files):
        path = PurePosixPath(name)
        if path.name == "go.mod":
            reader.go(name)
        elif path.name == "package.json":
            reader.npm(name)
        elif path.name == "pyproject.toml":
            reader.pyproject(name)
        elif path.suffix in {".txt", ".in"} and (
            path.stem.startswith("requirements") or "requirements" in path.parts[:-1]
        ):
            reader.requirements(name, _requirements_group(path))
    return ManifestReport(
        tuple(sorted(reader.paths)),
        tuple(
            sorted(
                reader.declarations,
                key=lambda d: (d.group, d.name, d.span.path, d.span.start),
            )
        ),
        tuple(reader.diagnostics),
    )
