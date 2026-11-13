import os
from dataclasses import dataclass, field, replace
from functools import lru_cache

from pydantic import TypeAdapter

from repopilot.artifacts import Artifacts
from repopilot.audit import Audit, analyze_audit
from repopilot.grounding import (
    DEFAULT_MODEL,
    Evidence,
    StructuredModel,
    grounded_answer,
)
from repopilot.module_graph import ModuleGraph
from repopilot.python_facts import Diagnostic, Module, Span
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore

GROUP_SIZE = 6


@dataclass(frozen=True)
class Role:
    module: str
    path: str
    description: str
    ranges: tuple[Span, ...]
    generated: bool = False


@dataclass
class PackageTree:
    directories: dict[str, "PackageTree"] = field(default_factory=dict)
    files: list[Role] = field(default_factory=list)

    @classmethod
    def build(cls, roles: list[Role]) -> "PackageTree":
        root = cls()
        for role in roles:
            parent = root
            for part in role.path.split("/")[:-1]:
                parent = parent.directories.setdefault(part, cls())
            parent.files.append(role)
        return root


def static_role(module: Module) -> Role:
    path = module.span.path
    if module.docstring and module.doc_span:
        return Role(
            module.name,
            path,
            module.docstring.split("\n\n")[0][:500],
            (module.doc_span,),
        )
    definitions = [
        s
        for s in module.symbols
        if s.kind in {"class", "function"} and s.name.rpartition(".")[0] == module.name
    ][:3]
    if definitions:
        names = ", ".join(d.name.rsplit(".", 1)[-1] for d in definitions)
        return Role(
            module.name, path, f"Defines {names}.", tuple(d.span for d in definitions)
        )
    if module.imports:
        return Role(
            module.name,
            path,
            f"Imports {module.imports[0].module}.",
            (module.imports[0].span,),
        )
    if module.symbols:
        symbol = module.symbols[0]
        return Role(
            module.name,
            path,
            f"Defines {symbol.name.rsplit('.', 1)[-1]}.",
            (symbol.span,),
        )
    return Role(
        module.name,
        path,
        ("Python package initializer." if module.is_package else "Python module.")
        if path.endswith(".py") else "Source module.",
        (module.span,),
    )


class Architecture:
    def __init__(
        self,
        cache: SnapshotCache,
        store: StateStore,
        model: StructuredModel | None = None,
        artifacts: Artifacts | None = None,
    ) -> None:
        self.cache = cache
        self.store = store
        self.model = model
        self.artifacts = artifacts or Artifacts()
        self.model_name = os.getenv("REPOPILOT_CHAT_MODEL") or DEFAULT_MODEL
        self.inventory = lru_cache(maxsize=4)(self._inventory)
        self.graph = lru_cache(maxsize=4)(self._graph)
        self.audit = lru_cache(maxsize=4)(self._audit)

    def _audit(self, repository: str, sha: str) -> Audit:
        snapshot = self.cache.open(repository, sha)
        versions = self.artifacts.versions
        return self.artifacts.read(
            snapshot.database.parent,
            "audit",
            f"1.{self.artifacts.facts_version(snapshot)}.{versions.dependencies}",
            TypeAdapter(Audit),
            lambda: analyze_audit(
                snapshot,
                self.artifacts.python(snapshot),
                self.artifacts.dependencies(snapshot),
            ),
        )

    def _graph(self, repository: str, sha: str) -> ModuleGraph:
        return ModuleGraph(self.artifacts.python(self.cache.open(repository, sha)))

    def _inventory(
        self, repository: str, sha: str
    ) -> tuple[tuple[Role, ...], tuple[Diagnostic, ...]]:
        facts = self.artifacts.python(self.cache.open(repository, sha))
        return tuple(static_role(m) for m in facts.modules), facts.diagnostics

    def roles(self, repository: str, sha: str) -> list[Role]:
        roles, _ = self.inventory(repository, sha)
        return self.describe_roles(repository, sha, roles)

    def describe_roles(
        self, repository: str, sha: str, roles: tuple[Role, ...]
    ) -> list[Role]:
        saved = self.store.module_roles(
            repository, sha, self.artifacts.versions.generated_key(self.model_name)
        )
        result = []
        for role in roles:
            if role.path in saved:
                description, ranges = saved[role.path]
                role = replace(
                    role,
                    description=description,
                    generated=True,
                    ranges=tuple(
                        Span(role.path, first, last) for first, last in ranges
                    ),
                )
            result.append(role)
        return result

    def evidence(
        self, repository: str, sha: str, role: Role, *, bounded: bool = False
    ) -> list[Evidence]:
        lines = self.cache.open(repository, sha).lines(role.path)
        result = []
        for span in role.ranges:
            if not lines:
                break
            end = min(span.end, span.start + 7) if bounded else span.end
            excerpt = "\n".join(lines[span.start - 1 : end])
            result.append(
                Evidence(repository, sha, Span(role.path, span.start, end), excerpt)
            )
        return result

    def generate(self, repository: str, sha: str, path: str) -> str:
        roles, _ = self.inventory(repository, sha)
        index = next((i for i, role in enumerate(roles) if role.path == path), None)
        if index is None:
            raise FileNotFoundError(path)
        start = index // GROUP_SIZE * GROUP_SIZE
        batch = roles[start : start + GROUP_SIZE]
        evidence = [
            item
            for role in batch
            for item in self.evidence(repository, sha, role, bounded=True)
            if len(item.excerpt) <= 1600
        ]
        answer = grounded_answer(
            "Describe the role of each evidenced source module. Give one short claim per module, "
            "citing only evidence from that module.",
            evidence,
            model=self.model,
        )
        if answer.status != "ok":
            return answer.status
        updates = {}
        for claim in answer.claims:
            source_path = claim.sources[0].span.path
            if all(e.span.path == source_path for e in claim.sources):
                updates[source_path] = (
                    claim.text,
                    [(e.span.start, e.span.end) for e in claim.sources],
                )
        self.store.save_module_roles(
            repository,
            sha,
            self.artifacts.versions.generated_key(self.model_name),
            updates,
        )
        return "ok" if updates else "insufficient_evidence"
