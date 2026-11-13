import json
from pathlib import Path

import httpx2
from langchain_core.messages import BaseMessage
from test_history import DATE, REPO, SHA, Remote

from repopilot.ask import Ask
from repopilot.history import History
from repopilot.snapshots import SnapshotCache
from repopilot.state import StateStore
from repopilot.timeline import timeline
from repopilot.vectors import VectorIndex

REASON = "We reverted the loader change because it broke input validation."
EDIT = "We reverted the loader change because it failed on empty files."


class Discussion(Remote):
    def respond(self, request: httpx2.Request) -> httpx2.Response:
        result = super().respond(request)
        if result.status_code != 200:
            return result
        data = result.json()
        path = request.url.path
        if path.endswith("/timeline"):
            for row in data:
                if row["event"] == "commented":
                    row.update(
                        body=REASON if self.version == 1 else EDIT,
                        user={"login": "maintainer"},
                    )
        elif path.endswith("/files"):
            data = [{"filename": "src/demo.py"}]
        elif path.endswith("/commits"):
            data[1]["sha"] = SHA[:8] + "b" * 32
            data[1]["html_url"] = f"https://github.com/{REPO}/commit/{data[1]['sha']}"
        return httpx2.Response(
            200,
            json=data,
            headers={k: v for k, v in result.headers.items() if k in {"etag", "link"}},
        )


class HistoryModel:
    def __init__(self) -> None:
        self.claim: str | None = None
        self.calls = 0

    def invoke(self, messages: list[BaseMessage]) -> object:
        self.calls += 1
        content = messages[-1].content
        assert isinstance(content, str)
        evidence = json.loads(content)["evidence"]
        sources = [
            s
            for s in evidence
            if s.get("record") == "Comment 19" or s.get("path") == "src/demo.py"
        ]
        text = self.claim or sources[0]["excerpt"].splitlines()[-1]
        return {"claims": [{"text": text, "evidence_ids": [s["id"] for s in sources]}]}


def test_reason_requires_explicit_source_and_preserves_the_observation(
    tmp_path: Path, explorer_url: str
) -> None:
    remote = Discussion()
    with httpx2.Client(transport=httpx2.MockTransport(remote.respond)) as client:
        history = History(tmp_path, client, lambda: 1000)
        cache = SnapshotCache(tmp_path / "sources")
        model = HistoryModel()
        ask = Ask(
            cache,
            StateStore(tmp_path / "state.db"),
            VectorIndex(tmp_path, cache),
            model,
            history,
        )

        def status(question: str) -> str:
            return ask.saved(REPO, SHA, ask.run(REPO, SHA, question))[1].status

        assert status("Why was #3 closed?") == "history_unavailable"
        history.load(REPO, SHA, 3)
        history.load(REPO, SHA)
        assert status("Why was #3 closed?") == "insufficient_evidence"
        assert status("Why was PR #11 rejected?") == "insufficient_evidence"
        assert (
            status("What changed in aaaaaaa?") == "ambiguous_revision"
            and not model.calls
        )
        question = "Why was the change reverted?"
        saved = ask.run(REPO, SHA, question, target="#3")
        answer = ask.saved(REPO, SHA, saved)[1]
        (claim,) = answer.claims
        assert claim.text == REASON and claim.sources[0].span.path == "src/demo.py"
        old = claim.history[0]
        assert old.author == "maintainer" and old.timestamp == DATE
        assert old.url.endswith("/issues/3#issuecomment-19")
        remote.version = 2
        history.load(REPO, SHA, 3, refresh=True)
        newer = ask.saved(REPO, SHA, ask.run(REPO, SHA, question, target=SHA))[
            1
        ].claims[0]
        assert newer.text == EDIT and newer.history[0].id != old.id
        assert ask.saved(REPO, SHA, saved)[1] == answer
        model.claim = "The author preferred another design."
        assert status("Why was #3 reverted?") == "insufficient_evidence"
        assert timeline(history, "other/demo", SHA, question, "#3").unavailable
        assert timeline(history, REPO, SHA, "Show the timeline", "").evidence
