import re
from dataclasses import dataclass
from itertools import zip_longest

from repopilot.grounding import Source
from repopilot.retrieval import CONTEXT_CHARS, SOURCE_LIMIT
from repopilot.timeline import timeline_requested

_DOCS = re.compile(
    r"\b(?:official|publisher|documentation|docs|library (?:API|guide)|API reference)\b",
    re.IGNORECASE,
)
_LOCAL = re.compile(
    r"\b(?:our |this |the )?(?:repository|repo|codebase|implementation)\b|\b(?:this project|our code|in the code)\b|\b[\w/-]+\.(?:py|js|ts|go)\b",
    re.IGNORECASE,
)
_USAGE = re.compile(
    r"\b(?:API|how (?:do|can|should) (?:I|we) use|how to|usage|parameters?|examples?|recommended)\b",
    re.IGNORECASE,
)
_TERMS = {
    "a",
    "an",
    "and",
    "api",
    "are",
    "as",
    "at",
    "by",
    "can",
    "code",
    "compare",
    "do",
    "does",
    "documentation",
    "docs",
    "for",
    "from",
    "guidance",
    "how",
    "in",
    "is",
    "it",
    "of",
    "official",
    "on",
    "or",
    "our",
    "project",
    "recommend",
    "repository",
    "say",
    "the",
    "this",
    "to",
    "use",
    "uses",
    "what",
    "which",
    "with",
}


@dataclass(frozen=True)
class Route:
    code: bool
    history: bool
    docs: bool
    packages: tuple[str, ...]


def route(question: str, target: str, catalog: tuple[str, ...]) -> Route:
    terms = set(re.findall(r"[\w.-]+", question.casefold().replace("_", "-")))
    packages = tuple(
        name
        for name in catalog
        if any(
            term.rstrip(".") == name or term.startswith(name + ".") for term in terms
        )
    )
    history = timeline_requested(question, target)
    docs = bool(_DOCS.search(question) or packages and _USAGE.search(question))
    return Route(
        bool(_LOCAL.search(question) or not (history or docs)), history, docs, packages
    )


def document_query(question: str, packages: tuple[str, ...]) -> str:
    words = dict.fromkeys(re.findall(r"\w+", question.casefold()))
    return " ".join(
        word for word in words if word not in _TERMS and word not in packages
    )


def passage(excerpt: str, query: str) -> str:
    terms = set(query.split())
    sentences = [
        s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", excerpt) if s.strip()
    ]
    return max(
        sentences,
        key=lambda s: len(terms.intersection(re.findall(r"\w+", s.casefold()))),
        default=excerpt,
    )


def budget(groups: list[list[Source]], *, limit: int = SOURCE_LIMIT, char_limit: int = CONTEXT_CHARS) -> list[Source]:
    selected: dict[str, Source] = {}
    remaining = char_limit
    for group in zip_longest(*groups):
        for item in group:
            if item is None or item.id in selected or len(item.excerpt) > remaining:
                continue
            selected[item.id] = item
            remaining -= len(item.excerpt)
            if len(selected) == limit:
                return list(selected.values())
    return list(selected.values())
