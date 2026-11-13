import re
import tomllib
from dataclasses import dataclass
from difflib import unified_diff

from repopilot.dependencies import Dependency, DependencyReport, known_providers
from repopilot.manifests import toml_pairs
from repopilot.python_facts import Span
from repopilot.snapshots import SourceSnapshot


@dataclass(frozen=True)
class PatchPreview:
    reason: str
    path: str = ""
    diff: str = ""
    evidence: Span | None = None


def source_diff(path: str, before: str, after: str) -> str:
    chunks = unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in chunks
    )


def _edit_project(text: str, name: str, newline: str) -> str | None:
    raw = text.encode()
    for keys, node in toml_pairs(raw):
        if keys != ("project", "dependencies") or node.type != "array":
            continue
        items = [item for item in node.named_children if item.type != "comment"]
        quote = raw[items[0].start_byte : items[0].start_byte + 1] if items else b'"'
        addition = quote + name.encode() + quote
        body = raw[node.start_byte + 1 : node.end_byte - 1]
        if b"\n" in body:
            first_line, _, _ = body.partition(b"\n")
            if first_line.lstrip().startswith((b'"', b"'")):
                # The first item shares the opening bracket's line.
                offset = items[0].start_byte
                addition += b", "
            else:
                offset = node.start_byte + 2 + len(first_line)
                example = items[0].start_byte if items else node.end_byte - 1
                indent = raw[raw.rfind(b"\n", 0, example) + 1 : example]
                if indent.strip():
                    return None
                addition = (
                    indent
                    + (b"" if items else b"    ")
                    + addition
                    + b","
                    + newline.encode()
                )
        else:
            offset = items[0].start_byte if items else node.end_byte - 1
            if items:
                addition += b", "
        updated = (raw[:offset] + addition + raw[offset:]).decode()
        before = tomllib.loads(text)["project"]["dependencies"]
        if tomllib.loads(updated)["project"]["dependencies"] == [name, *before]:
            return updated
    return None


def dependency_fix(
    snapshot: SourceSnapshot, report: DependencyReport, package: Dependency
) -> PatchPreview:
    if package.declarations:
        return PatchPreview("This distribution is already declared.")
    evidence = next(
        (
            u.fact.span
            for u in package.uses
            if not u.is_test
            and not u.fact.dynamic
            and known_providers(u.fact) == (package.name,)
        ),
        None,
    )
    if evidence is None:
        return PatchPreview(
            "A unique, known distribution mapping and a direct runtime import are required."
        )
    manifests = report.manifests
    roots = [p for p in manifests.paths if p in {"requirements.txt", "pyproject.toml"}]
    if (
        len(roots) != 1
        or manifests.diagnostics
        or any(p.endswith("/pyproject.toml") for p in manifests.paths)
    ):
        return PatchPreview(
            "A single, unambiguous root manifest with readable static declarations is required."
        )
    path = roots[0]
    text = snapshot.read_text(path)
    ends = set(re.findall(r"\r\n|\r|\n", text))
    if len(ends) > 1 or "\r" in ends:
        return PatchPreview(
            "The manifest uses inconsistent or unsupported line endings."
        )
    newline = next(iter(ends), "\n")
    if path == "requirements.txt":
        if "--hash" in text or text.rstrip().endswith("\\"):
            return PatchPreview(
                "The manifest uses hashes or a continued line that needs manual review."
            )
        separator = newline if text and not text.endswith("\n") else ""
        updated = (
            text
            + separator
            + package.name
            + (newline if not text or text.endswith("\n") else "")
        )
    else:
        updated_project = _edit_project(text, package.name, newline)
        if updated_project is None:
            return PatchPreview(
                "The root project needs an editable static project.dependencies array."
            )
        updated = updated_project
    # SourceSnapshot decodes UTF-8 BOMs but retains each entry's original byte count.
    size = next(e.size for e in snapshot.tree() if e.path == path)
    bom = "\ufeff" if size == len(text.encode()) + 3 else ""
    return PatchPreview(
        "Add the distribution used by this import.",
        path,
        source_diff(path, bom + text, bom + updated),
        evidence,
    )
