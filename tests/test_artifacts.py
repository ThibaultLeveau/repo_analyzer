import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from test_architecture import Descriptions

from repopilot.app import create_app
from repopilot.architecture import Architecture
from repopilot.artifacts import AnalysisVersions, Artifacts
from repopilot.dependencies import analyze_dependencies
from repopilot.guide import Guides
from repopilot.languages import analyze_snapshot
from repopilot.onboarding import extract_setup
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore

REPO = "Owner/Demo"
SHA = "a" * 40


def test_cache_resumes_recovers_and_scopes_parser_changes(
    tmp_path: Path, explorer_url: str
) -> None:
    cache = SnapshotCache(tmp_path / "sources")
    snap = cache.open(REPO, SHA)
    with (
        patch("repopilot.artifacts.analyze_snapshot", wraps=analyze_snapshot) as python,
        patch(
            "repopilot.artifacts.analyze_dependencies", wraps=analyze_dependencies
        ) as dependencies,
        patch("repopilot.artifacts.extract_setup", wraps=extract_setup) as setup,
    ):

        def prepare(_: int) -> None:
            Artifacts().prepare(snap)

        with ThreadPoolExecutor(3) as workers:
            list(workers.map(prepare, range(3)))
        first = Artifacts().dependencies(snap)
        assert first == analyze_dependencies(snap)
        assert (python.call_count, dependencies.call_count, setup.call_count) == (
            1,
            1,
            1,
        )
        with TestClient(
            create_app(data_dir=tmp_path), base_url="http://localhost"
        ) as client:
            for route in ("architecture", "dependencies", "getting-started"):
                assert (
                    client.get(explorer_url.removesuffix("code") + route).status_code
                    == 200
                )
        assert (python.call_count, dependencies.call_count, setup.call_count) == (
            1,
            1,
            1,
        )
        Artifacts(AnalysisVersions(setup=2)).prepare(snap)
        assert (python.call_count, dependencies.call_count, setup.call_count) == (
            1,
            1,
            2,
        )
        Artifacts(AnalysisVersions(python=2)).prepare(snap)
        assert (python.call_count, dependencies.call_count, setup.call_count) == (
            2,
            2,
            2,
        )
        with closing(sqlite3.connect(snap.database.parent / "analysis.db")) as db, db:
            db.execute(
                "UPDATE artifacts SET payload='{}' WHERE kind='dependencies' AND version='1.1'"
            )
        assert Artifacts().dependencies(snap) == first and dependencies.call_count == 3
        Artifacts().prepare(cache.open(REPO, "b" * 40))
        assert (python.call_count, dependencies.call_count, setup.call_count) == (
            3,
            4,
            3,
        )


def test_model_and_prompt_versions_reuse_original_rows(
    tmp_path: Path, explorer_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = SnapshotCache(tmp_path / "sources")
    store = StateStore(tmp_path / "state.db")
    model = Descriptions()
    monkeypatch.setenv("REPOPILOT_CHAT_MODEL", "fixture")
    original = Architecture(cache, store, model)
    assert original.generate(REPO, SHA, "src/demo.py") == "ok" and len(model.calls) == 1
    guides = Guides(original)
    item = guides.base(REPO, SHA).sections[0].groups["Inferred steps"][0]
    store.save_guide_notes(
        REPO, SHA, "fixture", [("Use the requirements file.", [item.source.id])]
    )
    reopened = Architecture(cache, StateStore(store.path), model)
    assert any(r.generated for r in reopened.roles(REPO, SHA))
    assert Guides(reopened).get(REPO, SHA).sections[0].notes
    changed = Architecture(
        cache, store, model, Artifacts(AnalysisVersions(roles_prompt=2))
    )
    assert not any(r.generated for r in changed.roles(REPO, SHA))
    assert Guides(changed).get(REPO, SHA).sections[0].notes
    guide_change = Architecture(
        cache, store, model, Artifacts(AnalysisVersions(guide_prompt=2))
    )
    assert not Guides(guide_change).get(REPO, SHA).sections[0].notes
    monkeypatch.setenv("REPOPILOT_CHAT_MODEL", "other")
    other = Architecture(cache, store, model)
    assert not any(r.generated for r in other.roles(REPO, SHA))
    assert not Guides(other).get(REPO, SHA).sections[0].notes
    assert len(model.calls) == 1
