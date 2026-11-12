from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from repopilot.app import create_app


def test_independent_apps_serve_health_offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    tmp_path.chmod(0o555)
    try:
        first, second = create_app(), create_app()
        assert first is not second
        for app in (first, second):
            with TestClient(app) as client:
                response = client.get("/health")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
        assert list(tmp_path.iterdir()) == []
    finally:
        tmp_path.chmod(0o755)
