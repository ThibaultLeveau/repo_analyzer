import json
from pathlib import Path

import httpx2
from journey_fixture import FILES, REPO, Catalog, CatalogChat, CatalogVectors
from test_imports import terminal

from repopilot.architecture import Architecture
from repopilot.ask import Ask
from repopilot.dependencies import analyze_dependencies
from repopilot.imports import import_service
from repopilot.issues import IssuePages
from repopilot.onboarding import extract_setup
from repopilot.vectors import VectorIndex


def analyze(directory: Path, remote: Catalog) -> dict[str, list[object]]:
    with import_service(
        directory, transport=httpx2.MockTransport(remote.respond)
    ) as service:
        job = service.submit(f"https://github.com/{REPO}")
        assert terminal(service.store, job.id).sha == remote.sha
        snap = service.cache.open(REPO, remote.sha)
        role = Architecture(service.cache, service.store).roles(REPO, remote.sha)[0]
        package = analyze_dependencies(snap).packages["pyyaml"]
        span = package.uses[0].fact.span
        (install,) = extract_setup(snap).documented
        (issue,) = IssuePages(directory, service.client).load(REPO).issues
        vectors = VectorIndex(directory, service.cache, CatalogVectors())
        assert vectors.build(REPO, remote.sha) == len(FILES)
        ask = Ask(service.cache, service.store, vectors, CatalogChat())
        saved = ask.run(
            REPO, remote.sha, "How does load_catalog parse the configuration?"
        )
        _, answer = ask.saved(REPO, remote.sha, saved)
        (claim,) = answer.claims
        (citation,) = claim.sources
        assert citation.sha == remote.sha and citation.repository == REPO
        return {
            "module": [role.module, role.description],
            "dependency": [package.name, package.observation, span.path, span.start],
            "installation": [install.text, install.span.path, install.span.start],
            "issue": [issue.number, issue.title],
            "answer": [
                claim.text,
                citation.span.path,
                citation.span.start,
                citation.span.end,
            ],
        }


def test_pinned_analysis_matches_golden_on_import_and_restart(tmp_path: Path) -> None:
    remote = Catalog()
    expected = json.loads(
        (Path(__file__).parent / "fixtures/catalog-analysis.json").read_text()
    )
    assert analyze(tmp_path, remote) == expected
    assert analyze(tmp_path, remote) == expected
    assert remote.issue_requests == 1
    changed = Catalog(
        FILES
        | {
            "src/catalog.py": FILES["src/catalog.py"].replace(
                "import yaml", "\nimport yaml"
            )
        }
    )
    assert changed.sha != remote.sha
    updated = analyze(tmp_path, changed)
    assert {key for key in updated if updated[key] != expected[key]} == {
        "dependency",
        "answer",
    }
    assert updated["dependency"][-1] == 3 and updated["answer"][-1] == 6
