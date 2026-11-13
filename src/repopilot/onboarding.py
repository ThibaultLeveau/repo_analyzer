import re
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal

import yaml
from markdown_it import MarkdownIt
from yaml.nodes import MappingNode, ScalarNode

from repopilot.manifests import toml_pairs
from repopilot.python_facts import Span
from repopilot.snapshots import SourceSnapshot

Category = Literal["Prerequisite", "Install", "Configure", "Run", "Test"]
Origin = Literal["Documented", "Inferred"]


@dataclass(frozen=True)
class Step:
    category: Category
    text: str
    origin: Origin
    span: Span
    platform: str = ""
    is_command: bool = True
    verified: bool = False


@dataclass(frozen=True)
class SetupEvidence:
    documented: tuple[Step, ...]
    inferred: tuple[Step, ...]

    @property
    def empty(self) -> bool:
        return not self.documented and not self.inferred


_SECTIONS: tuple[tuple[str, Category], ...] = (
    (r"prerequisites?|requirements?", "Prerequisite"),
    (r"tests?|testing|quality checks", "Test"),
    (r"install(?:ation|ing)?|dependencies", "Install"),
    (r"configuration|configure|environment|setup|set up", "Configure"),
    (r"run(?:ning)?|usage|launch|start", "Run"),
)
_COMMANDS: tuple[tuple[str, Category], ...] = (
    (
        r"(?:^|\s)(?:pytest|unittest|tox|nox|ruff|bandit|mypy)(?:\s|$)|^(?:npm|make|cargo|go) test\b",
        "Test",
    ),
    (
        r"(?:^|\s)(?:pip3?|uv pip|poetry|npm|pdm|playwright) install\b|^uv sync\b",
        "Install",
    ),
    (
        r"^(?:export |set |\$env:|source |\. |.*[\\/]activate(?:\.ps1)?\b)|(?:^|\s)-m venv\b",
        "Configure",
    ),
    (
        r"^(?:python[3]?|py|uvicorn|flask|docker(?:-compose)?|poetry run|uv run|npm run|make|go run)\b",
        "Run",
    ),
)


def _category(text: str, patterns: tuple[tuple[str, Category], ...]) -> Category | None:
    return next(
        (
            category
            for regex, category in patterns
            if re.search(regex, text, re.IGNORECASE)
        ),
        None,
    )


def _platform(text: str) -> str:
    if re.search(r"windows|powershell|pwsh|^cmd$|activate\.ps1", text, re.IGNORECASE):
        return "Windows"
    if re.search(r"posix|linux|macos|bash|zsh|^sh$|/bin/activate", text, re.IGNORECASE):
        return "POSIX"
    return ""


def _readme(snapshot: SourceSnapshot, path: str) -> Iterator[Step]:
    lines = snapshot.lines(path)
    headings: list[tuple[int, str]] = []
    tokens = MarkdownIt("commonmark").parse("\n".join(lines))
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            depth = int(token.tag[1:])
            headings = [(n, h) for n, h in headings if n < depth]
            headings.append((depth, tokens[index + 1].content))
            continue
        if token.map is None:
            continue
        first, last = token.map
        context = next(
            (c for _, h in reversed(headings) if (c := _category(h, _SECTIONS))), None
        )
        platform = next((p for _, h in reversed(headings) if (p := _platform(h))), "")
        if token.type in {"fence", "code_block"}:
            language = token.info.strip().split(" ", 1)[0].lower()
            if language not in {
                "",
                "sh",
                "shell",
                "console",
                "bash",
                "zsh",
                "powershell",
                "pwsh",
                "ps1",
                "cmd",
                "bat",
                "shell-session",
            }:
                continue
            commands = token.content.splitlines()
            offset = first + (2 if token.type == "fence" else 1)
            start = 0
            while start < len(commands):
                stop = start
                while stop + 1 < len(commands) and commands[stop].rstrip().endswith(
                    ("\\", "`", "^")
                ):
                    stop += 1
                text = "\n".join(commands[start : stop + 1]).strip()
                text = re.sub(r"^(?:\$ |PS [^>]*> )", "", text)
                category = _category(text, _COMMANDS) or context
                if (
                    text
                    and not text.startswith("#")
                    and category
                    and category != "Prerequisite"
                ):
                    yield Step(
                        category,
                        text,
                        "Documented",
                        Span(path, start + offset, stop + offset),
                        _platform(text) or _platform(language) or platform,
                    )
                start = stop + 1
        elif token.type == "inline" and tokens[index - 1].type == "paragraph_open":
            for number in range(first, last):
                line = lines[number]
                if context == "Prerequisite" and line.strip():
                    yield Step(
                        context,
                        re.sub(r"^\s*[-*+]\s+", "", line).strip(),
                        "Documented",
                        Span(path, number + 1, number + 1),
                        _platform(line) or platform,
                        False,
                    )
                else:
                    plain = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", line).strip()
                    if re.match(
                        r"^(?:Requires?\s+|Use\s+Python\s+\d|Python\s+\d.*(?:required|or later))",
                        plain,
                        re.IGNORECASE,
                    ):
                        yield Step(
                            "Prerequisite",
                            plain,
                            "Documented",
                            Span(path, number + 1, number + 1),
                            platform,
                            False,
                        )
                    elif re.match(
                        r"^(?:set|configure|define)\s+`", plain, re.IGNORECASE
                    ):
                        yield Step(
                            "Configure",
                            plain,
                            "Documented",
                            Span(path, number + 1, number + 1),
                            platform,
                            False,
                        )
                    elif (
                        context
                        and "`" not in line
                        and (kind := _category(plain, _COMMANDS))
                        and not plain.endswith(".")
                    ):
                        yield Step(
                            kind,
                            plain,
                            "Documented",
                            Span(path, number + 1, number + 1),
                            _platform(plain) or platform,
                        )
                    for match in re.finditer(r"`([^`]+)`", line):
                        code = match[1]
                        category = _category(code, _COMMANDS)
                        if category:
                            yield Step(
                                category,
                                code,
                                "Documented",
                                Span(path, number + 1, number + 1),
                                _platform(code) or _platform(line) or platform,
                            )


def _project(snapshot: SourceSnapshot, path: str) -> Iterator[Step]:
    data = snapshot.read_text(path).encode()
    try:
        pairs = list(toml_pairs(data))
    except ValueError:
        return
    found_test = False
    for keys, node in pairs:
        span = Span(path, node.start_point.row + 1, node.end_point.row + 1)
        if keys == ("project", "name"):
            yield Step("Install", "python -m pip install .", "Inferred", span)
        elif keys == ("project", "requires-python"):
            version = tomllib.loads(
                "v=" + data[node.start_byte : node.end_byte].decode()
            )["v"]
            if isinstance(version, str):
                yield Step(
                    "Prerequisite",
                    f"Python {version}",
                    "Inferred",
                    span,
                    is_command=False,
                )
        elif len(keys) == 3 and re.fullmatch(r"[A-Za-z0-9][\w.-]*", keys[2]):
            if keys[:2] == ("project", "optional-dependencies"):
                yield Step(
                    "Install", f'python -m pip install ".[{keys[2]}]"', "Inferred", span
                )
            elif keys[:2] == ("project", "scripts") and node.type == "string":
                yield Step("Run", keys[2], "Inferred", span)
        if not found_test and keys[:2] == ("tool", "pytest"):
            yield Step("Test", "python -m pytest", "Inferred", span)
            found_test = True


def _compose(snapshot: SourceSnapshot, path: str) -> Iterator[Step]:
    try:
        root = yaml.compose(snapshot.read_text(path), Loader=yaml.SafeLoader)
    except (yaml.YAMLError, RecursionError):
        return
    if not isinstance(root, MappingNode):
        return
    for key, services in root.value:
        if (
            not isinstance(key, ScalarNode)
            or key.value != "services"
            or not isinstance(services, MappingNode)
        ):
            continue
        for service, _ in services.value:
            if isinstance(service, ScalarNode) and re.fullmatch(
                r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", service.value
            ):
                yield Step(
                    "Run",
                    f"docker compose -f {path} up {service.value}",
                    "Inferred",
                    Span(path, service.start_mark.line + 1, service.end_mark.line + 1),
                )


def extract_setup(snapshot: SourceSnapshot) -> SetupEvidence:
    documented: list[Step] = []
    inferred: list[Step] = []
    for entry in snapshot.tree():
        path = entry.path
        if entry.kind != "text" or "/" in path:
            continue
        if path.lower() in {"readme", "readme.md", "readme.markdown"}:
            documented.extend(_readme(snapshot, path))
        elif path == "pyproject.toml":
            inferred.extend(_project(snapshot, path))
        elif path in {"run.py", "setup.py", "pytest.ini", "requirements.txt"}:
            candidates: dict[str, tuple[Category, str]] = {
                "run.py": ("Run", "python run.py"),
                "setup.py": ("Install", "python -m pip install ."),
                "pytest.ini": ("Test", "python -m pytest"),
                "requirements.txt": (
                    "Install",
                    "python -m pip install -r requirements.txt",
                ),
            }
            number = next(
                (n for n, line in enumerate(snapshot.lines(path), 1) if line.strip()),
                None,
            )
            if number:
                category, command = candidates[path]
                inferred.append(
                    Step(category, command, "Inferred", Span(path, number, number))
                )
        elif path in {
            "compose.yml",
            "compose.yaml",
            "docker-compose.yml",
            "docker-compose.yaml",
        }:
            inferred.extend(_compose(snapshot, path))
    return SetupEvidence(tuple(documented), tuple(inferred))
