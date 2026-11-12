from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app


def test_independent_apps_serve_health_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    project.chmod(0o555)
    try:
        first, second = (create_app(data_dir=tmp_path / "data") for _ in range(2))
        assert first is not second
        for app in (first, second):
            with TestClient(app, base_url="http://localhost") as client:
                response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
        assert list(project.iterdir()) == []
    finally:
        project.chmod(0o755)
