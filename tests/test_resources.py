from collections.abc import Callable
from pathlib import Path

import psutil
import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app
from repopilot.resources import HISTORY, INTERVAL, ResourceSampler, Sample, disk_bytes


def test_sampler_keeps_spaced_readings_across_restarts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current = [0.0]
    sampler = ResourceSampler(tmp_path, lambda: current[0])

    def read(now: float) -> Sample:
        return Sample(now, None if now == 0 else 25.0, int(now), None, int(now) * 10)

    monkeypatch.setattr(sampler, "read", read)
    for moment in (0.0, 20.0, 60.0, 100.0):
        current[0] = moment
        sampler.capture()
    assert sampler.history(0) == [read(60), read(0)]
    reopened = ResourceSampler(tmp_path, lambda: current[0])
    assert reopened.history(1) == [read(60)]
    current[0] = HISTORY * INTERVAL + 61
    monkeypatch.setattr(reopened, "read", read)
    reopened.capture()
    assert reopened.history(0) == [read(current[0])]


def test_sizes_exclude_runtime_and_handle_unreadable_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, size in (
        ("state.db", 7),
        ("sources/snap/source.db", 11),
        ("sources/snap/source.db-wal", 3),
        ("runtime/ignored.db", 50),
        ("chroma/n/index.bin", 23),
    ):
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"x" * size)
    assert disk_bytes(tmp_path, chroma=False) == 21
    assert disk_bytes(tmp_path, chroma=True) == 23
    assert disk_bytes(tmp_path / "missing", chroma=True) == 0

    def denied(*args: object, onerror: Callable[[OSError], None]) -> None:
        onerror(PermissionError())

    monkeypatch.setattr("repopilot.resources.os.walk", denied)
    sampler = ResourceSampler(tmp_path, lambda: 1)

    def inaccessible() -> None:
        raise psutil.AccessDenied()

    monkeypatch.setattr(sampler.process, "cpu_percent", inaccessible)
    monkeypatch.setattr(sampler.process, "memory_info", inaccessible)
    assert sampler.read(1) == Sample(1, None, None, None, None)


def test_empty_metrics_and_unknown_usage(tmp_path: Path, explorer_url: str) -> None:
    url = explorer_url.removesuffix("/code") + "/observability"
    with TestClient(
        create_app(data_dir=tmp_path), base_url="http://localhost"
    ) as client:
        result = client.get(url)
        assert result.is_success
        assert "No import jobs" in result.text and "Unknown" in result.text
        assert "Unavailable" in result.text
        assert client.get(url, params={"window": "bad"}).status_code == 422
        assert client.get(url.replace("Demo", "Missing")).status_code == 404
