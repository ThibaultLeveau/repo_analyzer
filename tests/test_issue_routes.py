import re
from pathlib import Path

import httpx2
from fastapi.testclient import TestClient
from test_issues import Clock, Pages

from repopilot.app import create_app


def test_issue_routes_read_only_on_get_and_enforce_cooldown(
    tmp_path: Path, explorer_url: str
) -> None:
    fake, clock = Pages(), Clock()
    url = explorer_url.removesuffix("/code") + "/issues"
    with TestClient(
        create_app(
            data_dir=tmp_path, transport=httpx2.MockTransport(fake), clock=clock
        ),
        base_url="http://localhost",
    ) as client:
        opened = client.get(url)
        token = re.search(r'name="csrf_token" value="([^"]+)"', opened.text)
        assert token and client.get(url, params={"label": "bug"}).status_code == 200
        assert not fake.calls
        form = {"csrf_token": token[1]}
        for route, data in (
            ("/load", {"action": "load"}),
            ("/interval", {"hours": "1"}),
        ):
            assert (
                client.post(
                    url + route, data=data | {"csrf_token": "invalid"}
                ).status_code
                == 403
            )
        for value in ("0", "-3", "inf", "not-a-number"):
            assert (
                client.post(url + "/interval", data=form | {"hours": value}).status_code
                == 422
            )
        assert (
            client.post(url + "/load", data=form | {"action": "unknown"}).status_code
            == 422
        )
        assert not fake.calls
        assert (
            client.post(url + "/load", data=form | {"action": "load"}).status_code
            == 200
        )
        assert (
            client.post(url + "/load", data=form | {"action": "extend"}).status_code
            == 409
        )
        filtered = client.get(
            url, params={"label": "bug", "number": 148}, headers={"HX-Request": "true"}
        )
        assert "Details for 148" in filtered.text and "<html" not in filtered.text
        assert client.get(url, params={"number": 999}).status_code == 404
        assert client.get(url.replace("a" * 40, "f" * 40)).status_code == 404
        assert fake.calls == [1]
