import re
from dataclasses import dataclass, replace
from functools import lru_cache
from urllib.parse import quote

from repopilot.architecture import Architecture, Role
from repopilot.grounding import Claim, Evidence, grounded_answer
from repopilot.onboarding import SetupEvidence
from repopilot.python_facts import Span


@dataclass(frozen=True)
class Item:
    label: str
    text: str
    source: Evidence
    command: bool = False
    verified: bool = False
    platform: str = ""

    @property
    def caption(self) -> str:
        parts = [self.label, self.platform]
        if self.command:
            parts.append("Verified" if self.verified else "Unverified")
        return " · ".join(part for part in parts if part)


@dataclass(frozen=True)
class Section:
    title: str
    groups: dict[str, list[Item]]
    notes: tuple[Claim, ...] = ()


def _plain(text: str) -> str:
    # Inline prose stays literal even in a renderer that allows raw HTML.
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~&-])", r"\\\1", " ".join(text.split()))


def _link(source: Evidence) -> str:
    span = source.span
    label = _plain(f"{span.path} · L{span.start}–{span.end}")
    path = quote(span.path)
    return f"[{label}](https://github.com/{source.repository}/blob/{source.sha}/{path}#L{span.start}-L{span.end})"


@dataclass(frozen=True)
class Guide:
    repository: str
    sha: str
    sections: tuple[Section, ...]

    def markdown(self) -> str:
        lines = ["# Getting Started", "", f"{self.repository} · `{self.sha}`", ""]
        for section in self.sections:
            lines += [f"## {section.title}", ""]
            for note in section.notes:
                links = " ".join(_link(source) for source in note.sources)
                lines += [f"{_plain(note.text)} {links}", ""]
            for group, items in section.groups.items():
                lines += [f"### {group}", ""]
                if not items:
                    lines += ["No supporting evidence found.", ""]
                for item in items:
                    lines += [f"{_plain(item.caption)} · {_link(item.source)}", ""]
                    if item.command:
                        longest = max(
                            (len(m) for m in re.findall(r"`+", item.text)), default=0
                        )
                        fence = "`" * max(3, longest + 1)
                        lines += [fence, item.text, fence, ""]
                    else:
                        lines += [_plain(item.text), ""]
        return "\n".join(lines)


class Guides:
    def __init__(self, architecture: Architecture) -> None:
        self.architecture = architecture
        self.notes_key = architecture.artifacts.versions.generated_key(
            architecture.model_name, guide=True
        )
        self.base = lru_cache(maxsize=4)(self._base)

    def _base(self, repository: str, sha: str) -> Guide:
        snapshot = self.architecture.cache.open(repository, sha)
        setup = self.architecture.artifacts.setup(snapshot)
        roles, _ = self.architecture.inventory(repository, sha)
        return self.compose(repository, sha, setup, roles)

    def compose(
        self, repository: str, sha: str, setup: SetupEvidence, roles: tuple[Role, ...]
    ) -> Guide:
        snapshot = self.architecture.cache.open(repository, sha)
        source_lines = lru_cache(maxsize=12)(snapshot.lines)

        def source(span: Span) -> Evidence:
            lines = source_lines(span.path)
            excerpt = "\n".join(lines[span.start - 1 : min(span.end, span.start + 7)])
            return Evidence(repository, sha, span, excerpt[:1600])

        install = Section(
            "Installation", {"README instructions": [], "Inferred steps": []}
        )
        usage = Section("Usage", {"README instructions": [], "Inferred steps": []})
        first_files: list[Item] = []
        for group, steps in (
            ("README instructions", setup.documented),
            ("Inferred steps", setup.inferred),
        ):
            for step in steps:
                section = usage if step.category in {"Run", "Test"} else install
                section.groups[group].append(
                    Item(
                        step.category,
                        step.text,
                        source(step.span),
                        step.is_command,
                        step.verified,
                        step.platform,
                    )
                )
        for steps, context in (
            (setup.documented, "Read the documented setup instructions."),
            (setup.inferred, "Review the source of the inferred setup steps."),
        ):
            if steps:
                span = steps[0].span
                first_files.append(Item(span.path, context, source(span)))
        roles = tuple(
            sorted(
                roles,
                key=lambda r: (
                    r.path.rsplit("/", 1)[-1]
                    not in {"run.py", "main.py", "__main__.py", "app.py"},
                    r.path.startswith("tests/"),
                    r.path.count("/"),
                    r.path,
                ),
            )
        )
        for role in roles:
            if any(item.source.span.path == role.path for item in first_files):
                continue
            evidence = source(
                Span(
                    role.path,
                    min(s.start for s in role.ranges),
                    max(s.end for s in role.ranges),
                )
            )
            if evidence.excerpt.strip():
                first_files.append(Item(role.path, role.description, evidence))
            if len(first_files) == 5:
                break
        return Guide(
            repository,
            sha,
            (
                install,
                usage,
                Section("Architecture", {"First files to read": first_files}),
            ),
        )

    @staticmethod
    def _sources(guide: Guide) -> dict[str, Evidence]:
        return {
            item.source.id: item.source
            for section in guide.sections
            for items in section.groups.values()
            for item in items[:8]
        }

    def generate(self, repository: str, sha: str) -> str:
        guide = self.base(repository, sha)
        sources = self._sources(guide)
        answer = grounded_answer(
            "Organize the supplied facts into short onboarding notes for installation, usage and first files to read. "
            "Use one topic per note. Cite the relevant evidence. Leave all commands to the guide's command listings.",
            list(sources.values()),
            model=self.architecture.model,
        )
        if answer.status == "ok":
            self.architecture.store.save_guide_notes(
                repository,
                sha,
                self.notes_key,
                [(c.text, [s.id for s in c.sources]) for c in answer.claims],
            )
        return answer.status

    def get(self, repository: str, sha: str) -> Guide:
        return self.with_notes(self.base(repository, sha))

    def with_notes(self, guide: Guide) -> Guide:
        repository, sha = guide.repository, guide.sha
        saved = self.architecture.store.guide_notes(repository, sha, self.notes_key)
        sources = self._sources(guide)
        pending = [
            Claim(text, tuple(sources[k] for k in keys))
            for text, keys in saved
            if keys and all(k in sources for k in keys)
        ]
        sections = []
        for section in guide.sections:
            ids = {
                item.source.id for items in section.groups.values() for item in items
            }
            notes = tuple(n for n in pending if n.sources[0].id in ids)
            sections.append(replace(section, notes=notes))
            pending = [n for n in pending if n.sources[0].id not in ids]
        return replace(guide, sections=tuple(sections))
