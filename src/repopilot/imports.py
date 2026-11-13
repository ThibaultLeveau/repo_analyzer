import logging
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, TypedDict

import httpx2
from filelock import FileLock
from langgraph.graph import END, START, StateGraph

from repopilot.architecture import Architecture
from repopilot.artifacts import Artifacts
from repopilot.github import GitHubError, repository_name, resolve_repository
from repopilot.grounding import StructuredModel
from repopilot.models import RepositoryHead
from repopilot.snapshots import SnapshotCache, SnapshotError
from repopilot.state import Job, JobStateError, StateStore

if TYPE_CHECKING:
    from repopilot.vectors import Embedder

log = logging.getLogger(__name__)


class ImportState(TypedDict):
    job_id: int
    head: NotRequired[RepositoryHead]


class ImportService:
    def __init__(
        self,
        store: StateStore,
        cache: SnapshotCache,
        client: httpx2.Client,
        workers: ThreadPoolExecutor,
        *,
        model: StructuredModel | None = None,
        embedder: "Embedder | None" = None,
        artifacts: Artifacts | None = None,
    ) -> None:
        self.store = store
        self.cache = cache
        self.client = client
        self.workers = workers
        self.architecture = Architecture(cache, store, model, artifacts)
        self.embedder = embedder
        graph = StateGraph(ImportState)
        graph.add_node("resolve", self._resolve)
        graph.add_node("fetch", self._fetch)
        graph.add_node("publish", self._publish)
        graph.add_edge(START, "resolve")
        graph.add_edge("resolve", "fetch")
        graph.add_edge("fetch", "publish")
        graph.add_edge("publish", END)
        self.graph = graph.compile()

    def submit(self, url: str) -> Job:
        return self._schedule(repository_name(url))

    def retry(self, job_id: int) -> Job:
        job = self.store.job(job_id)
        if job.status != "failed":
            raise JobStateError("Only failed imports can be retried")
        head, _ = self.store.source(job.id)
        refresh = self.store.refresh_info(job_id)
        return self._schedule(
            refresh.previous_repo if refresh else job.repository,
            head,
            previous_sha=refresh.previous_sha if refresh else None,
        )

    def refresh(self, name: str, sha: str) -> Job:
        self.cache.open(name, sha)
        return self._schedule(name, previous_sha=self.store.active_sha(name) or sha)

    def _schedule(
        self,
        name: str,
        head: RepositoryHead | None = None,
        *,
        previous_sha: str | None = None,
    ) -> Job:
        job, created = self.store.submit(name, pinned=head, previous_sha=previous_sha)
        if created:
            self.workers.submit(self.run, job.id)
        return job

    def run(self, job_id: int) -> None:
        self.store.start(job_id)
        try:
            self.graph.invoke({"job_id": job_id})
        except (GitHubError, SnapshotError) as exc:
            self.store.fail(job_id, str(exc))
        except Exception:
            log.exception("Import job %d failed", job_id)
            self.store.fail(job_id, "Import failed while processing the repository")

    def _resolve(self, state: ImportState) -> dict[str, RepositoryHead]:
        job = self.store.job(state["job_id"])
        head, _ = self.store.source(job.id)
        if head is None:
            head = resolve_repository(
                f"https://github.com/{job.repository}", self.client
            )
            self.store.pin(job.id, head)
        return {"head": head}

    def _fetch(self, state: ImportState) -> None:
        self.cache.fetch(state["head"], self.client)
        self.store.mark_fetched(state["job_id"])

    def _publish(self, state: ImportState) -> None:
        job_id, head = state["job_id"], state["head"]
        info = self.store.refresh_info(job_id)
        manifest = None
        if info:
            from repopilot.refresh import Refresh
            from repopilot.vectors import VectorIndex

            vectors = VectorIndex(self.store.path.parent, self.cache, self.embedder)
            manifest = Refresh(self.architecture, vectors).prepare(
                head, info.previous_repo, info.previous_sha
            )
        self.store.complete(job_id, head.sha, manifest=manifest)


@contextmanager
def import_service(
    data_dir: Path,
    *,
    transport: httpx2.BaseTransport | None = None,
    model: StructuredModel | None = None,
    embedder: "Embedder | None" = None,
    artifacts: Artifacts | None = None,
) -> Iterator[ImportService]:
    data_dir.mkdir(parents=True, exist_ok=True)
    # Only the worker that owns this directory may recover interrupted jobs.
    with (
        FileLock(data_dir / "import.lock", timeout=0),
        httpx2.Client(transport=transport) as client,
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="repo-import") as workers,
    ):
        store = StateStore(data_dir / "state.db")
        service = ImportService(
            store,
            SnapshotCache(data_dir / "sources"),
            client,
            workers,
            model=model,
            embedder=embedder,
            artifacts=artifacts,
        )
        for job_id in store.recover_interrupted():
            workers.submit(service.run, job_id)
        yield service
