import html
import json
import string
from dataclasses import asdict, dataclass, field
from typing import Literal
from urllib.parse import quote

from markdown_it import MarkdownIt
from pydantic import TypeAdapter

from repopilot.architecture import Architecture, static_role
from repopilot.grounding import Answer
from repopilot.guide import Guides
from repopilot.python_facts import Span
from repopilot.scoring import Scores

Format = Literal["json", "md", "html"]
MEDIA_TYPES = {"json": "application/json", "md": "text/markdown", "html": "text/html"}


@dataclass(frozen=True)
class Citation:
    path: str
    start: int | None
    end: int | None
    url: str


@dataclass(frozen=True)
class Entry:
    title: str
    text: str
    evidence: tuple[Citation, ...] = ()
    timestamp: str | None = None


@dataclass(frozen=True, kw_only=True)
class AuditEntry(Entry):
    category: str
    severity: str
    rule: str


@dataclass(frozen=True)
class Report:
    repository: str
    sha: str
    snapshot_created_at: str | None
    sections: dict[str, list[Entry] | None] = field(default_factory=dict)
    schema_version: int = 1

    def render(self, format: Format) -> str:
        if format == "json":
            return json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n"
        lines = [
            f"# {_literal(self.repository)}",
            "",
            f"Commit `{self.sha}` · Report schema {self.schema_version}",
            "",
        ]
        if self.snapshot_created_at:
            lines.extend(
                [f"Snapshot imported {_literal(self.snapshot_created_at)}", ""]
            )
        for section, entries in self.sections.items():
            lines.extend([f"## {section}", ""])
            if not entries:
                lines.extend(
                    [
                        "Analysis unavailable."
                        if entries is None
                        else "No entries recorded.",
                        "",
                    ]
                )
            for entry in entries or ():
                lines.extend(
                    [f"### {_literal(entry.title)}", "", _literal(entry.text), ""]
                )
                if entry.timestamp:
                    lines.extend([_literal(entry.timestamp), ""])
                for source in entry.evidence:
                    label = _literal(
                        f"{source.path} · L{source.start}–{source.end}"
                        if source.start is not None
                        else source.path
                    )
                    lines.append(f"[{label}](<{source.url}>)")
                lines.append("")
        markdown = "\n".join(lines)
        if format == "md":
            return markdown
        if format != "html":
            raise ValueError("Unsupported report format")
        body = MarkdownIt("commonmark", {"html": False}).render(markdown)
        return (
            f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            f"<title>{html.escape(self.repository)} · Repository report</title>"
            "<style>body{margin:2rem auto;padding:0 1rem;max-width:70ch;font:1rem/1.55 system-ui;overflow-wrap:anywhere}h2{border-bottom:1px solid #888}a{color:#1761a6}</style>"
            f"</head><body><main>{body}</main></body></html>\n"
        )


def _literal(value: str) -> str:
    return "".join(
        "\\" + c if c in string.punctuation else c for c in " ".join(value.split())
    )


def build_report(
    architecture: Architecture, scores: Scores, repository: str, sha: str
) -> Report:
    snapshot = architecture.cache.open(repository, sha)
    facts, deps, setup, audit = architecture.artifacts.saved_analysis(snapshot)
    store = architecture.store
    imported = next(
        (s.created_at for s in store.snapshots(repository) if s.sha == sha), None
    )
    report = Report(repository, sha, imported)

    def cite(span: Span) -> Citation:
        url = f"https://github.com/{repository}/blob/{sha}/{quote(span.path)}#L{span.start}-L{span.end}"
        return Citation(span.path, span.start, span.end, url)

    roles = tuple(static_role(m) for m in facts.modules) if facts else ()
    report.sections["Architecture"] = (
        [
            Entry(r.module, r.description, tuple(cite(s) for s in r.ranges))
            for r in architecture.describe_roles(repository, sha, roles)
        ]
        if facts
        else None
    )
    dependencies: list[Entry] = []
    if deps:
        for package in deps.packages.values():
            versions = ", ".join(
                d.constraint or "unconstrained" for d in package.declarations
            )
            spans = [d.span for d in package.declarations] + [
                u.fact.span for u in package.uses
            ]
            dependencies.append(
                Entry(
                    package.name,
                    f"{package.observation}. {versions}".strip(),
                    tuple(cite(s) for s in dict.fromkeys(spans)),
                )
            )
    report.sections["Dependencies"] = dependencies if deps else None
    report.sections["Quality audit"] = (
        [
            AuditEntry(
                f.title,
                f"{f.category} · {f.rule} · {f.severity}. {f.explanation} {f.limit}",
                (cite(f.span),),
                category=f.category,
                severity=f.severity,
                rule=f.rule,
            )
            for f in audit.findings
        ]
        if audit
        else None
    )
    if audit and audit.failed_specialists:
        report.sections["Audit status"] = [
            Entry("Partial audit", "Unavailable specialists: " + ", ".join(audit.failed_specialists))
        ]
    onboarding: list[Entry] = []
    if setup:
        guides = Guides(architecture)
        guide = guides.with_notes(guides.compose(repository, sha, setup, roles))
        for section in guide.sections:
            for group, items in section.groups.items():
                onboarding.extend(
                    Entry(
                        f"{section.title} · {group} · {item.caption}",
                        item.text,
                        (cite(item.source.span),),
                    )
                    for item in items
                )
            onboarding.extend(
                Entry(
                    section.title, note.text, tuple(cite(s.span) for s in note.sources)
                )
                for note in section.notes
            )
    report.sections["Getting Started"] = onboarding if setup else None
    questions: list[Entry] = []
    for question, payload, timestamp in store.saved_questions(repository, sha):
        answer = TypeAdapter(Answer).validate_json(payload)
        if answer.claims:
            questions.extend(
                Entry(
                    question,
                    claim.text,
                    tuple(cite(s.span) for s in claim.sources)
                    + tuple(
                        Citation(f"{s.label} · {s.updated_at}", None, None, s.url)
                        for s in claim.history
                    )
                    + tuple(
                        Citation(f"Official documentation · {s.package} · {s.edition} · {s.section}", None, None, s.url)
                        for s in claim.documentation
                    ) + tuple(
                        Citation(f"User note · {s.filename} · {s.section} · revision {s.revision}", None, None, s.url)
                        for s in claim.notes
                    ),
                    timestamp,
                )
                for claim in answer.claims
            )
        else:
            questions.append(Entry(question, answer.status, timestamp=timestamp))
    report.sections["Questions and answers"] = questions
    assessments = []
    for issue, revision in scores.snapshot_scores(repository, sha):
        score = revision.score
        factors = " ".join(f"{f.name} (+{f.points}): {f.reason}" for f in score.factors)
        components = ", ".join(score.components) or "Unknown"
        claims = " ".join(c.text for c in score.reasoning.claims)
        desc = (
            f"{score.difficulty} · {score.score if score.score is not None else 'Unscored'} · "
            f"Confidence: {score.confidence} · {score.rubric}. Components: {components}. {factors} {claims}"
        ).strip()
        sources = [
            *score.evidence,
            *(s for claim in score.reasoning.claims for s in claim.sources),
        ]
        assessments.append(
            Entry(
                f"Issue ID {issue} · {revision.title}"
                if revision.title
                else f"Issue ID {issue}",
                desc,
                tuple(cite(span) for span in dict.fromkeys(s.span for s in sources)),
                revision.evaluated_at,
            )
        )
    report.sections["Issue assessments"] = assessments
    return report
