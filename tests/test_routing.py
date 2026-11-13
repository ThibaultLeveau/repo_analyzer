import json
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage
from test_grounding import FakeModel
from test_manifests import manifest_snapshot
from test_official_docs import Publisher

from repopilot.ask import Ask
from repopilot.dependencies import analyze_dependencies
from repopilot.grounding import (
    DocumentationEvidence,
    Evidence,
    NoteEvidence,
    Source,
    grounded_answer,
)
from repopilot.official_docs import DEFAULTS, FRESH
from repopilot.python_facts import Span
from repopilot.retrieval import CONTEXT_CHARS, SOURCE_LIMIT
from repopilot.routing import Route, budget, passage, route
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.vectors import VectorIndex

REPO, SHA = "owner/repo", "a" * 40
MIXED = "How does our code parse the frobnicator and what do the PyYAML docs recommend?"


class SourcesModel:
    def __init__(self) -> None:
        self.calls: list[set[str]] = []

    def invoke(self, messages: list[BaseMessage]) -> object:
        payload = messages[-1].content
        assert isinstance(payload, str)
        items = json.loads(payload)["evidence"]
        self.calls.append({item["source"] for item in items})
        claims = []
        for kind in ("code", "documentation"):
            selected = next((item for item in items if item["source"] == kind), None)
            if selected:
                claims.append(
                    {
                        "text": selected["excerpt"].splitlines()[0],
                        "evidence_ids": [selected["id"]],
                    }
                )
        return {"claims": claims}


def test_routing_decisions_and_shared_budget() -> None:
    catalog = tuple(DEFAULTS)
    assert route("What do my notes say about dispatch?", "", catalog) == Route(False, False, False, (), True)
    assert route("Compare our code to my notes.", "", catalog) == Route(True, False, False, (), True)
    assert route("Where is parse_items defined?", "", catalog) == Route(
        True, False, False, ()
    )
    assert route("Why was #12 reverted?", "", catalog) == Route(False, True, False, ())
    assert route("How do I use requests.get?", "", catalog) == Route(
        False, False, True, ("requests",)
    )
    assert route(MIXED, "PR #1", catalog) == Route(True, True, True, ("pyyaml",))
    code = [Evidence(REPO, SHA, Span(f"{i}.py", 1, 1), "x" * 1200) for i in range(20)]
    doc = DocumentationEvidence(
        REPO,
        SHA,
        "pkg",
        "==1",
        "1",
        "Guide",
        "Intro",
        "https://example.org",
        0,
        "z" * 1400,
    )
    pool: list[list[Source]] = [list(code), [doc] * 4]
    selected = budget(pool)
    assert selected[:2] == [code[0], doc]
    assert len({x.id for x in selected}) == len(selected) <= SOURCE_LIMIT
    assert sum(len(x.excerpt) for x in selected) <= CONTEXT_CHARS
    note = NoteEvidence(REPO, SHA, "attachment", "rev", "notes.md", "Enqueue", "/local#heading-1", 0, "Use a queue.")
    mixed = budget([list(code), [note]])
    assert note in mixed and sum(len(x.excerpt) for x in mixed) <= CONTEXT_CHARS
    with pytest.raises(ValueError, match="same snapshot"):
        grounded_answer("Compare", [code[0], replace(note, sha="b"*40)])
    assert (
        passage("Install the tool. Use a decoder for input.", "decoder input")
        == "Use a decoder for input."
    )


def test_docs_mixed_and_missing_sources_are_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = manifest_snapshot(
        tmp_path / "sources",
        {
            "requirements.txt": "PyYAML==6.0.1\nDjango==5.2.1",
            "parse.py": "def frobnicator():\n    return 42\n",
        },
    )
    cache = SnapshotCache(tmp_path / "sources")
    model = SourcesModel()
    pub = Publisher(tmp_path)
    deps = analyze_dependencies(snapshot).packages
    for dep in ("pyyaml", "django"):
        pub.docs.index(REPO, SHA, deps[dep])
    assert pub.docs.search(REPO, SHA, "frobnicator", 1)[0][0] == "django"
    assert pub.docs.search(REPO, SHA, "", 1, ("pyyaml",))[0][0] == "pyyaml"
    ask = Ask(
        cache,
        StateStore(tmp_path / "state.db"),
        VectorIndex(tmp_path, cache),
        model,
        docs=pub.docs,
    )

    def answer(question: str) -> tuple[str, ...]:
        saved = ask.run(REPO, SHA, question)
        result = ask.saved(REPO, SHA, saved)[1]
        return (result.status, *result.notices)

    assert answer("What do the PyYAML docs say about parsing?") == ("ok",)
    assert model.calls[-1] == {"documentation"}
    saved = ask.run(REPO, SHA, MIXED)
    result = ask.saved(REPO, SHA, saved)[1]
    assert result.status == "ok" and model.calls[-1] == {"code", "documentation"}
    local, external = result.claims
    assert local.sources and not local.documentation and not external.sources
    (doc,) = external.documentation
    assert (
        doc.package == "pyyaml"
        and doc.url.endswith("#parsing")
        and doc.version == "==6.0.1"
    )
    pub.now += FRESH
    assert answer("What do PyYAML docs recommend?") == ("docs_unavailable",)
    partial = answer(MIXED)
    assert (
        partial[0] == "ok"
        and "official documentation" in partial[1]
        and model.calls[-1] == {"code"}
    )
    assert ask.saved(REPO, SHA, saved)[1] == result
    ask.model = FakeModel(
        {
            "claims": [
                {
                    "text": "The publisher recommends this implementation.",
                    "evidence_ids": [local.sources[0].id],
                }
            ]
        }
    )
    assert answer(MIXED)[0] == "insufficient_evidence"
    ask.model = model
    paraphrase = FakeModel(
        {
            "claims": [
                {
                    "text": "The model paraphrased the publisher.",
                    "evidence_ids": [doc.id],
                }
            ]
        }
    )
    assert grounded_answer(MIXED, [doc], model=paraphrase).claims[0].text == doc.excerpt
    mixed_claim = FakeModel(
        {
            "claims": [
                {"text": doc.excerpt, "evidence_ids": [doc.id, local.sources[0].id]}
            ]
        }
    )
    assert (
        grounded_answer(MIXED, [doc, local.sources[0]], model=mixed_claim).status
        == "insufficient_evidence"
    )

    def timeout(*args: object, **kwargs: object) -> None:
        raise TimeoutError

    monkeypatch.setattr(pub.docs, "search", timeout)
    assert answer(MIXED) == partial
    assert "GitHub history" in answer("Where in the repo is frobnicator after #22?")[1]
    with pytest.raises(ValueError, match="same snapshot"):
        grounded_answer(
            MIXED, [local.sources[0], replace(doc, sha="b" * 40)], model=model
        )
