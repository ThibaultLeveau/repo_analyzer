import hashlib
import io
import json
import zipfile

import httpx2
from langchain_core.messages import BaseMessage

from repopilot.vectors import EmbeddingConfig

REPO = "Example/Catalog"
FILES = {
    "README.md": "# Catalog\n## Installation\n```sh\npython -m pip install -r requirements.txt\n```\n",
    "requirements.txt": "PyYAML==6.0.3\n",
    "src/catalog.py": '"""Read the catalog configuration."""\nimport yaml\n\ndef load_catalog(source):\n    return yaml.safe_load(source)\n',
    "tests/test_catalog.py": 'from catalog import load_catalog\n\ndef test_config():\n    assert load_catalog("active: true") == {"active": True}\n',
}


class Catalog:
    def __init__(self, files: dict[str, str] | None = None) -> None:
        files = FILES if files is None else files
        self.sha = hashlib.sha1(
            json.dumps(files, sort_keys=True).encode(), usedforsecurity=False
        ).hexdigest()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as writer:
            for path, contents in files.items():
                writer.writestr("catalog/" + path, contents)
        self.archive = archive.getvalue()
        self.issue_requests = 0

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.lower()
        if (
            request.url.host == "codeload.github.com"
            and path == f"/example/catalog/zip/{self.sha}"
        ):
            return httpx2.Response(200, stream=httpx2.ByteStream(self.archive))
        if path == "/repos/example/catalog":
            return httpx2.Response(
                200,
                json={"private": False, "full_name": REPO, "default_branch": "main"},
            )
        if path == "/repos/example/catalog/commits/main":
            return httpx2.Response(200, json={"sha": self.sha})
        if path == "/repos/example/catalog/issues":
            self.issue_requests += 1
            return httpx2.Response(
                200,
                json=[
                    {
                        "id": 81,
                        "number": 7,
                        "title": "Document the catalog format",
                        "body": "Add a short YAML example.",
                        "state": "open",
                        "labels": [{"name": "documentation"}],
                        "created_at": "2026-01-02T12:00:00Z",
                        "updated_at": "2026-01-03T12:00:00Z",
                    }
                ],
            )
        raise AssertionError(f"Unexpected fixture request: {path}")


class CatalogVectors:
    def embed(self, texts: list[str], config: EmbeddingConfig) -> list[list[float]]:
        return [
            [
                float("yaml.safe_load" in s or "How does" in s),
                float("load_catalog" in s),
            ]
            + [0.0] * (config.dimensions - 2)
            for s in texts
        ]


class CatalogChat:
    def invoke(self, messages: list[BaseMessage]) -> object:
        payload = messages[-1].content
        assert isinstance(payload, str)
        evidence = json.loads(payload)["evidence"]
        source = next(
            item
            for item in evidence
            if item["path"] == "src/catalog.py" and "yaml.safe_load" in item["excerpt"]
        )
        return {
            "claims": [
                {
                    "text": "It parses the configuration with yaml.safe_load.",
                    "evidence_ids": [source["id"]],
                }
            ]
        }
