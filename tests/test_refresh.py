import json
from pathlib import Path
from threading import Event

import httpx2
from journey_fixture import FILES, REPO, Catalog
from test_ask import CitedModel
from test_imports import terminal
from test_vectors import Vectors

from repopilot.artifacts import AnalysisVersions, Artifacts
from repopilot.guide import Guides
from repopilot.imports import import_service
from repopilot.vectors import VectorIndex


class Upstream:
    def __init__(self) -> None:
        self.catalog = Catalog()
        self.downloads = 0
        self.resolves = 0
        self.block = False
        self.started = Event()
        self.release = Event()

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "codeload.github.com":
            self.downloads += 1
            if self.block:
                self.started.set()
                assert self.release.wait(10)
                return httpx2.Response(503)
        if "/commits/" in request.url.path:
            self.resolves += 1
        return self.catalog.respond(request)


def test_refresh_reuses_or_rebuilds_then_publishes_atomically(tmp_path: Path) -> None:
    upstream = Upstream()
    transport = httpx2.MockTransport(upstream.respond)
    provider = Vectors()
    model = CitedModel()
    a = upstream.catalog.sha
    with import_service(
        tmp_path, transport=transport, embedder=provider, model=model
    ) as engine:
        assert (
            terminal(engine.store, engine.submit(f"https://github.com/{REPO}").id).sha
            == a
        )
        index = VectorIndex(tmp_path, engine.cache, provider)
        assert Guides(engine.architecture).generate(REPO, a) == "ok"
        assert engine.architecture.generate(REPO, a, "src/catalog.py") == "ok"
        assert model.calls == 2
        assert index.build(REPO, a) == len(FILES)
        assert len(provider.batches) == 1
        again = terminal(engine.store, engine.refresh(REPO, a).id)
        assert again.sha == a and engine.store.active_sha(REPO) == a
        assert model.calls == 2
        info = engine.store.refresh_info(again.id)
        assert info and info.manifest and json.loads(info.manifest)["same_commit"]
        assert (upstream.resolves, upstream.downloads, len(provider.batches)) == (
            2,
            1,
            1,
        )

    versions = AnalysisVersions(setup=2)
    with import_service(
        tmp_path,
        transport=transport,
        embedder=provider,
        model=model,
        artifacts=Artifacts(versions),
    ) as engine:
        changed_config = terminal(engine.store, engine.refresh(REPO, a).id)
        info = engine.store.refresh_info(changed_config.id)
        assert (
            info
            and info.manifest
            and json.loads(info.manifest)["versions"]["setup"] == 2
        )
        assert model.calls == 3
        assert (upstream.resolves, upstream.downloads, len(provider.batches)) == (
            3,
            1,
            1,
        )
        upstream.catalog = Catalog(FILES | {"extra.txt": "Another commit."})
        b = upstream.catalog.sha
        upstream.block = True
        refresh = engine.refresh(REPO, a)
        try:
            assert upstream.started.wait(10)
            assert (
                engine.refresh(REPO, a).id == refresh.id
                and engine.store.active_sha(REPO) == a
            )
        finally:
            upstream.release.set()
        assert terminal(engine.store, refresh.id).status == "failed"
        assert engine.store.active_sha(REPO) == a
        upstream.block = False
        retry = terminal(engine.store, engine.retry(refresh.id).id)
        assert retry.sha == b and engine.store.active_sha(REPO) == b
        assert model.calls == 5
        assert (
            engine.cache.open(REPO, a).read_text("src/catalog.py")
            == FILES["src/catalog.py"]
        )
        info = engine.store.refresh_info(retry.id)
        assert (
            info
            and info.previous_sha == a
            and info.manifest
            and json.loads(info.manifest)["vectors_built"]
        )
        assert (upstream.resolves, upstream.downloads, len(provider.batches)) == (
            4,
            3,
            2,
        )
        assert upstream.catalog.issue_requests == 0
