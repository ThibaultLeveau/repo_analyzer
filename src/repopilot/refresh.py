import json
from dataclasses import asdict

from repopilot.architecture import Architecture
from repopilot.artifacts import CHUNK_SCHEMA
from repopilot.github import GitHubError
from repopilot.guide import Guides
from repopilot.models import RepositoryHead
from repopilot.vectors import VectorError, VectorIndex


class Refresh:
    def __init__(self, architecture: Architecture, vectors: VectorIndex) -> None:
        self.architecture = architecture
        self.guides = Guides(architecture)
        self.vectors = vectors

    def prepare(
        self, head: RepositoryHead, previous_repo: str, previous_sha: str
    ) -> str:
        repo, sha = head.full_name, head.sha
        architecture = self.architecture
        store = architecture.store
        architecture.artifacts.prepare(architecture.cache.open(repo, sha))
        paths, guide, vectors = store.refresh_features(previous_repo, previous_sha)
        versions = architecture.artifacts.versions
        role_key = versions.generated_key(architecture.model_name)
        available = {r.path for r in architecture.inventory(repo, sha)[0]}
        required = paths & available
        saved = store.module_roles(repo, sha, role_key)
        pending = required - saved.keys()
        while pending:
            path = min(pending)
            status = architecture.generate(repo, sha, path)
            saved = store.module_roles(repo, sha, role_key)
            if status != "ok" or path not in saved:
                raise GitHubError(
                    f"Module descriptions are incomplete ({status}). Retry refresh."
                )
            pending -= saved.keys()
        generate_notes = guide and not store.guide_notes(
            repo, sha, self.guides.notes_key
        )
        if generate_notes:
            status = self.guides.generate(repo, sha)
            if status not in {"ok", "insufficient_evidence"}:
                raise GitHubError(
                    f"Guide notes are unavailable ({status}). Retry refresh."
                )
        vectors = vectors or self.vectors.ready(previous_repo, previous_sha)
        build_vectors = vectors and not self.vectors.ready(repo, sha)
        if vectors:
            try:
                if build_vectors:
                    self.vectors.build(repo, sha)
            except VectorError as exc:
                raise GitHubError(str(exc)) from exc
            store.remember_vectors(repo, sha)
        return json.dumps(
            {
                "schema": 1,
                "versions": asdict(versions),
                "model": architecture.model_name,
                "embedding": asdict(self.vectors.config),
                "chunks": CHUNK_SCHEMA,
                "same_commit": sha == previous_sha,
                "roles": len(required),
                "guide_generated": bool(generate_notes),
                "vectors_built": bool(build_vectors),
            },
            sort_keys=True,
        )
