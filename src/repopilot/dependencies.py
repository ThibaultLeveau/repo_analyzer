from collections import defaultdict
from dataclasses import dataclass
from pathlib import PurePosixPath

from packaging.utils import canonicalize_name

from repopilot.manifests import Declaration, ManifestReport, parse_manifests
from repopilot.python_facts import ImportFact, PythonFacts, analyze_python
from repopilot.snapshots import SourceSnapshot

_PROVIDERS = {
    "PIL": ("pillow",),
    "yaml": ("pyyaml",),
    "bs4": ("beautifulsoup4",),
    "sklearn": ("scikit-learn",),
    "dateutil": ("python-dateutil",),
    "dotenv": ("python-dotenv",),
    "cv2": (
        "opencv-python",
        "opencv-python-headless",
        "opencv-contrib-python",
        "opencv-contrib-python-headless",
    ),
    "google.cloud.storage": ("google-cloud-storage",),
    "google.auth": ("google-auth",),
}


@dataclass(frozen=True)
class Usage:
    fact: ImportFact
    is_test: bool
    candidates: tuple[str, ...] = ()


@dataclass(frozen=True)
class Dependency:
    name: str
    declarations: tuple[Declaration, ...]
    uses: tuple[Usage, ...]

    @property
    def files(self) -> int:
        return len({use.fact.span.path for use in self.uses if not use.candidates})

    @property
    def observation(self) -> str:
        if not self.uses:
            return "No static usage found"
        confirmed = [use for use in self.uses if not use.candidates]
        if not confirmed:
            return "Ambiguous distribution"
        if not self.declarations:
            return "Possibly undeclared"
        if all(use.is_test for use in confirmed):
            return "Used only in tests"
        if all(use.fact.dynamic for use in confirmed):
            return "Literal dynamic import"
        return "Import evidence found"


@dataclass(frozen=True)
class DependencyReport:
    manifests: ManifestReport
    packages: dict[str, Dependency]


def analyze_dependencies(
    snapshot: SourceSnapshot, *, facts: PythonFacts | None = None
) -> DependencyReport:
    manifests = parse_manifests(snapshot)
    declarations: dict[str, list[Declaration]] = defaultdict(list)
    uses: dict[str, list[Usage]] = defaultdict(list)
    for declaration in manifests.declarations:
        declarations[declaration.name].append(declaration)

    for module in (facts or analyze_python(snapshot)).modules:
        path = PurePosixPath(module.span.path)
        is_test = (
            bool({"test", "tests"} & set(path.parts[:-1]))
            or path.name.startswith("test_")
            or path.name.endswith("_test.py")
        )
        for fact in module.imports:
            if fact.kind != "external" or not fact.target:
                continue
            root = fact.target.split(".")[0]
            imported = f"{fact.target}.{fact.name}" if fact.name else fact.target
            providers = next(
                (
                    names
                    for key, names in _PROVIDERS.items()
                    if imported == key or imported.startswith(key + ".")
                ),
                (canonicalize_name(root),),
            )
            candidates = tuple(p for p in providers if p in declarations) or providers
            ambiguous = candidates if len(candidates) > 1 else ()
            for name in candidates:
                uses[name].append(Usage(fact, is_test, ambiguous))
    packages = {
        name: Dependency(
            name, tuple(declarations.get(name, ())), tuple(uses.get(name, ()))
        )
        for name in sorted(declarations.keys() | uses.keys())
    }
    return DependencyReport(manifests, packages)
