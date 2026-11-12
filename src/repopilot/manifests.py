import posixpath
import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

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


class _Reader:
    def __init__(self, snapshot: SourceSnapshot) -> None:
        self.snapshot = snapshot
        self.files = {e.path: e.kind for e in snapshot.tree() if e.kind != "directory"}
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
        return self.snapshot.read_text(path)

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
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            self.diagnostics.append(Diagnostic(path, None, str(exc)))
            return
        data = text.encode()
        lines = text.split("\n")
        tree = Parser(Language(tree_sitter_toml.language())).parse(data)
        if tree.root_node.has_error:
            self.diagnostics.append(
                Diagnostic(path, None, "Unsupported TOML syntax for source locations")
            )
            return
        groups: dict[str, Node] = {}
        for keys, value in _pairs(tree.root_node, data):
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


def parse_manifests(snapshot: SourceSnapshot) -> ManifestReport:
    reader = _Reader(snapshot)
    for name in sorted(reader.files):
        path = PurePosixPath(name)
        if path.name == "pyproject.toml":
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
