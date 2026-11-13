import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from filelock import FileLock
from pydantic import TypeAdapter, ValidationError

from repopilot.audit import Audit
from repopilot.dependencies import DependencyReport, analyze_dependencies
from repopilot.events import measure
from repopilot.javascript import SUFFIXES
from repopilot.languages import analyze_snapshot
from repopilot.onboarding import SetupEvidence, extract_setup
from repopilot.python_facts import PythonFacts
from repopilot.snapshots import SourceSnapshot

_FACTS = TypeAdapter(PythonFacts)
_DEPENDENCIES = TypeAdapter(DependencyReport)
_SETUP = TypeAdapter(SetupEvidence)
CHUNK_SCHEMA = 1


@dataclass(frozen=True)
class AnalysisVersions:
    python: int = 1
    dependencies: int = 1
    setup: int = 1
    roles_prompt: int = 1
    guide_prompt: int = 1

    def generated_key(self, model: str, *, guide: bool = False) -> str:
        versions = (
            (self.python, self.setup, self.guide_prompt)
            if guide
            else (self.python, self.roles_prompt)
        )
        # The original tables used just the model name.
        return (
            model if all(v == 1 for v in versions) else json.dumps([model, *versions])
        )


class Artifacts:
    def __init__(self, versions: AnalysisVersions = AnalysisVersions()) -> None:
        self.versions = versions

    @staticmethod
    @lru_cache(maxsize=32)
    def _has_javascript(snapshot: SourceSnapshot) -> bool:
        return any(
            e.kind != "directory"
            and (e.path.endswith(SUFFIXES) or Path(e.path).name == "package.json")
            for e in snapshot.tree()
        )

    def facts_version(self, snapshot: SourceSnapshot) -> str:
        version = str(self.versions.python)
        return version + "-js1" if self._has_javascript(snapshot) else version

    def read[T](
        self,
        directory: Path,
        kind: str,
        version: str,
        schema: TypeAdapter[T],
        compute: Callable[[], T],
    ) -> T:
        with (
            measure("cache.read") as metrics,
            FileLock(directory / f"{kind.partition(':')[0]}.lock"),
            closing(sqlite3.connect(directory / "analysis.db")) as db,
        ):
            db.execute(
                "CREATE TABLE IF NOT EXISTS artifacts (kind TEXT,version TEXT,payload BLOB NOT NULL,PRIMARY KEY(kind,version))"
            )
            row = db.execute(
                "SELECT payload FROM artifacts WHERE kind=? AND version=?",
                (kind, version),
            ).fetchone()
            if row is not None:
                try:
                    cached = schema.validate_json(row[0], strict=True)
                    metrics.cache = "hit"
                    return cached
                except ValidationError:
                    pass
            metrics.cache = "miss"
            value = compute()
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO artifacts VALUES (?,?,?)",
                    (kind, version, schema.dump_json(value)),
                )
            return value

    def python(self, snapshot: SourceSnapshot) -> PythonFacts:
        return self.read(
            snapshot.database.parent,
            "python",
            self.facts_version(snapshot),
            _FACTS,
            lambda: analyze_snapshot(snapshot),
        )

    def dependencies(self, snapshot: SourceSnapshot) -> DependencyReport:
        version = f"{self.facts_version(snapshot)}.{self.versions.dependencies}"
        return self.read(
            snapshot.database.parent,
            "dependencies",
            version,
            _DEPENDENCIES,
            lambda: analyze_dependencies(snapshot, facts=self.python(snapshot)),
        )

    def setup(self, snapshot: SourceSnapshot) -> SetupEvidence:
        return self.read(
            snapshot.database.parent,
            "setup",
            str(self.versions.setup),
            _SETUP,
            lambda: extract_setup(snapshot),
        )

    def saved_analysis(
        self, snapshot: SourceSnapshot
    ) -> tuple[
        PythonFacts | None, DependencyReport | None, SetupEvidence | None, Audit | None
    ]:
        database = snapshot.database.parent / "analysis.db"
        if not database.is_file():
            return None, None, None, None
        with closing(
            sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        ) as db:

            def saved[T](kind: str, version: str, schema: TypeAdapter[T]) -> T | None:
                row = db.execute(
                    "SELECT payload FROM artifacts WHERE kind=? AND version=?",
                    (kind, version),
                ).fetchone()
                if row is not None:
                    try:
                        return schema.validate_json(row[0], strict=True)
                    except ValidationError:
                        return None
                return None

            db.execute("BEGIN")
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
            ).fetchone():
                return None, None, None, None
            return (
                saved("python", self.facts_version(snapshot), _FACTS),
                saved(
                    "dependencies",
                    f"{self.facts_version(snapshot)}.{self.versions.dependencies}",
                    _DEPENDENCIES,
                ),
                saved("setup", str(self.versions.setup), _SETUP),
                saved(
                    "audit",
                    f"1.{self.facts_version(snapshot)}.{self.versions.dependencies}",
                    TypeAdapter(Audit),
                ),
            )

    def prepare(self, snapshot: SourceSnapshot) -> None:
        self.python(snapshot)
        self.dependencies(snapshot)
        self.setup(snapshot)
