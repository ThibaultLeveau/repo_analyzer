import json
from pathlib import Path

from langchain_core.messages import BaseMessage
from test_grounding import FakeModel
from test_manifests import manifest_snapshot

from repopilot.languages import analyze_snapshot
from repopilot.specialists import explain, parsed_modules, performance, security, style


class ExplainModel:
    def __init__(self) -> None:
        self.payload = ""

    def invoke(self, messages: list[BaseMessage]) -> object:
        content = messages[-1].content
        assert isinstance(content, str)
        self.payload = content
        return {
            "claims": [
                {
                    "text": "Review the static observation and its runtime limits.",
                    "evidence_ids": [e["id"]],
                }
                for e in json.loads(content)["evidence"]
            ]
        }


def test_specialists_cover_positive_negative_and_ambiguous_sites(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "executed"
    text = f"""import logging as log
import re as regex
logger = log.getLogger(__name__)
apiKey = 'never-send-this-fixture'
logger.info("value %s", apiKey)
logger.debug(f"received {{apiKey}}")
logger.info("key length", len(apiKey))
other.info(apiKey)
for item in items:
    regex.compile("private-pattern-fixture")
    regex.compile(item)
    def deferred():
        regex.compile("not in this loop")
regex.compile("outside")
def CamelCase(): pass
class Owner:
    def CamelCase(self): pass
def snake_case(): pass
open({str(marker)!r}, 'w').close()
"""
    snapshot = manifest_snapshot(
        tmp_path / "sources",
        {"sample.py": text, "broken.py": "def (", "other.txt": text},
    )
    facts = analyze_snapshot(snapshot)
    parsed = list(parsed_modules(snapshot, facts))
    assert len(parsed) == 1
    findings = security(parsed) + performance(parsed) + style(facts)
    assert [(f.rule, f.category, f.span.start, f.span.end) for f in findings] == [
        ("SEC001", "security", 5, 5),
        ("SEC001", "security", 6, 6),
        ("PERF001", "performance", 10, 10),
        ("STYLE001", "style", 15, 15),
    ]
    assert all(f.span.path == "sample.py" and f.limit for f in findings)
    assert not marker.exists()
    assert not security([]) and not performance([])
    model = ExplainModel()
    enriched = explain(findings, "owner/repo", "a" * 40, model)
    assert enriched == explain(findings, "owner/repo", "a" * 40, model)
    assert all(f.model_note and f.limit for f in enriched)
    for sensitive in (
        "never-send-this-fixture",
        "private-pattern-fixture",
        "apiKey",
        "not in this loop",
    ):
        assert sensitive not in model.payload and sensitive not in repr(enriched)
    assert (
        explain(findings, "owner/repo", "a" * 40, FakeModel({"invalid": "output"}))
        == findings
    )
    assert not marker.exists()
