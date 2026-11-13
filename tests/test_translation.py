import json
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from repopilot.grounding import Claim, Evidence
from repopilot.guide import Guide, Item, Section
from repopilot.python_facts import Span
from repopilot.translation import TranslationError, Translations


class Translator:
    def __init__(self) -> None:
        self.calls = 0
        self.invalid = False
        self.inputs: list[str] = []

    def invoke(self, messages: list[BaseMessage]) -> object:
        self.calls += 1
        data = messages[-1].content
        assert isinstance(data, str)
        self.inputs = json.loads(data)
        return {
            "texts": ["invalid"]
            if self.invalid
            else ["Consulter " + t for t in self.inputs]
        }


def test_prose_translation_preserves_identifiers_sources_and_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    source = Evidence(
        "owner/repo", "a" * 40, Span("src/main.py", 2, 4), "def dispatch():"
    )
    text = "Read `start(mode)` via src/main.py or https://example.org/run and dispatch."
    items = [
        Item("Run", "python -m app --check", source, command=True),
        Item("Read", text, source),
    ]
    guide = Guide(
        source.repository,
        source.sha,
        (Section("Usage", {"Steps": items}, (Claim("Check APP_MODE.", (source,)),)),),
    )
    store, model = Translations(tmp_path), Translator()
    assert store.get(guide, "fr", "m") is None
    with pytest.raises(TranslationError, match="OPENAI_API_KEY"):
        store.get(guide, "fr", "m", generate=True)
    with pytest.raises(TranslationError, match="supported"):
        store.get(guide, "invalid", "m", generate=True, model=model)
    model.invalid = True
    with pytest.raises(TranslationError):
        store.get(guide, "fr", "m", generate=True, model=model)
    assert store.get(guide, "fr", "m") is None
    model.invalid = False
    translated = store.get(
        guide, "fr", "m", generate=True, model=model, identifiers={"dispatch"}
    )
    assert translated and translated.sections[0].groups["Steps"][0] == items[0]
    assert translated.sections[0].groups["Steps"][1] == replace(
        items[1], text="Consulter " + text
    )
    assert translated.sections[0].notes[0] == Claim(
        "Consulter Check APP_MODE.", (source,)
    )
    assert all(
        x not in str(model.inputs)
        for x in [
            "APP_MODE",
            "src/main.py",
            "start(mode)",
            "dispatch",
            "example.org",
            "--check",
        ]
    )
    assert (
        Translations(tmp_path).get(guide, "fr", "m", generate=True, model=model)
        == translated
        and model.calls == 2
    )
    assert (
        store.get(guide, "de", "m") is None
        and store.get(guide, "fr", "new-model") is None
    )
    assert store.get(replace(guide, sections=()), "fr", "m") is None
