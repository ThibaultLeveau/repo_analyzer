from pathlib import Path

import pytest
from test_manifests import manifest_snapshot

from repopilot.call_flow import MAX_CALLS, trace
from repopilot.languages import analyze_snapshot
from repopilot.references import ReferenceIndex

SOURCE = """def start(callback):
    left()
    right(callback)
def left():
    start(None)
def right(callback):
    callback()
    third()
def third():
    fourth()
def fourth():
    fifth()
def fifth():
    pass
class Runner:
    def execute(self):
        left()
"""


def test_branches_cycles_dynamic_calls_and_bounded_depth(tmp_path: Path) -> None:
    snapshot = manifest_snapshot(tmp_path, {"app.py": SOURCE})
    index = ReferenceIndex(snapshot, analyze_snapshot(snapshot))
    flow = trace(index, "app.py", "app.start")
    assert flow == trace(index, "app.py", "app.start") and not flow.truncated
    assert [
        (c.destination, c.depth, c.parent_row, c.span.start, c.span.end, c.boundary)
        for c in flow.calls
    ] == [
        ("app.left", 1, 0, 2, 2, ""),
        ("app.start", 2, 1, 5, 5, "Cycle"),
        ("app.right", 1, 0, 3, 3, ""),
        ("callback", 2, 3, 7, 7, "Unresolved"),
        ("app.third", 2, 3, 8, 8, ""),
        ("app.fourth", 3, 5, 10, 10, ""),
        ("app.fifth", 4, 6, 12, 12, ""),
    ]
    extended = manifest_snapshot(
        tmp_path / "extended", {"app.py": SOURCE.replace("    pass", "    left()")}
    )
    bounded = trace(
        ReferenceIndex(extended, analyze_snapshot(extended)), "app.py", "app.start"
    )
    assert bounded.calls[-1].boundary == "Depth limit"
    assert (
        trace(index, "app.py", "app.Runner.execute").calls[0].destination == "app.left"
    )
    assert not trace(index, "app.py", "app.fifth").calls
    for path, name in (
        ("other.py", "app.start"),
        ("app.py", "app.Runner"),
        ("app.py", "app.missing"),
    ):
        with pytest.raises(KeyError):
            trace(index, path, name)


def test_large_fanout_stops_at_the_call_budget(tmp_path: Path) -> None:
    snapshot = manifest_snapshot(
        tmp_path, {"app.py": "def f():\n" + "    f()\n" * (MAX_CALLS + 2)}
    )
    flow = trace(
        ReferenceIndex(snapshot, analyze_snapshot(snapshot)), "app.py", "app.f"
    )
    assert flow.truncated and len(flow.calls) == MAX_CALLS
    assert all(c.boundary == "Cycle" for c in flow.calls)
