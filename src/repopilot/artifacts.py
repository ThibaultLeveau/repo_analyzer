import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock
from pydantic import TypeAdapter, ValidationError

from repopilot.dependencies import DependencyReport, analyze_dependencies
from repopilot.onboarding import SetupEvidence, extract_setup
from repopilot.python_facts import PythonFacts, analyze_python
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

    def _read[T](
        self,
        directory: Path,
        kind: str,
        version: str,
        schema: TypeAdapter[T],
        compute: Callable[[], T],
    ) -> T:
        with (
            FileLock(directory / f"{kind}.lock"),
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
                    return schema.validate_json(row[0], strict=True)
                except ValidationError:
                    pass
            value = compute()
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO artifacts VALUES (?,?,?)",
                    (kind, version, schema.dump_json(value)),
                )
            return value

    def python(self, snapshot: SourceSnapshot) -> PythonFacts:
        return self._read(
            snapshot.database.parent,
            "python",
            str(self.versions.python),
            _FACTS,
            lambda: analyze_python(snapshot),
        )

    def dependencies(self, snapshot: SourceSnapshot) -> DependencyReport:
        version = f"{self.versions.python}.{self.versions.dependencies}"
        return self._read(
            snapshot.database.parent,
            "dependencies",
            version,
            _DEPENDENCIES,
            lambda: analyze_dependencies(snapshot, facts=self.python(snapshot)),
        )

    def setup(self, snapshot: SourceSnapshot) -> SetupEvidence:
        return self._read(
            snapshot.database.parent,
            "setup",
            str(self.versions.setup),
            _SETUP,
            lambda: extract_setup(snapshot),
        )

    def prepare(self, snapshot: SourceSnapshot) -> None:
        self.python(snapshot)
        self.dependencies(snapshot)
        self.setup(snapshot)
