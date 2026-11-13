import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlencode

import httpx2
from packaging.specifiers import SpecifierSet
from pydantic import TypeAdapter

from repopilot.artifacts import Artifacts
from repopilot.github import GitHubError, api_get
from repopilot.manifests import Declaration, revision_declarations
from repopilot.snapshots import SourceSnapshot

REQUEST_LIMIT = 12


@dataclass(frozen=True)
class Attribution:
    description: str
    commit: str = ""
    date: str = ""
    line: int = 0
    source: str = ""


def _sha(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{40}", value):
        raise ValueError
    return value


def _version(declaration: Declaration) -> tuple[SpecifierSet | str, str | None]:
    if declaration.name.startswith(("npm:", "go:")):
        return declaration.constraint, declaration.url
    return SpecifierSet(declaration.constraint), declaration.url


def _find(text: str, selected: Declaration) -> Declaration | None:
    entries = [
        d
        for d in revision_declarations(selected.span.path, text, selected.group)
        if d.name == selected.name
    ]
    found = [
        d
        for d in entries
        if (d.group, d.extras, d.marker)
        == (selected.group, selected.extras, selected.marker)
    ]
    if len(found) > 1 or (entries and not found):
        raise ValueError
    return found[0] if found else None


def _trace(
    client: httpx2.Client, repo: str, sha: str, selected: Declaration
) -> Attribution:
    prefix = f"https://api.github.com/repos/{repo}"
    path = selected.span.path
    requests = 0

    def get(endpoint: str) -> httpx2.Response:
        nonlocal requests
        if requests >= REQUEST_LIMIT:
            raise ValueError
        requests += 1
        return api_get(client, prefix + endpoint)

    def declaration_at(commit: str) -> Declaration | None:
        data = get(f"/contents/{quote(path)}?ref={commit}").json()
        if (
            data["type"] != "file"
            or data["path"] != path
            or data["encoding"] != "base64"
            or not 0 <= data["size"] <= 1 << 20
        ):
            raise ValueError
        content = base64.b64decode("".join(data["content"].split()), validate=True)
        if len(content) != data["size"]:
            raise ValueError
        return _find(content.decode("utf-8-sig"), selected)

    cursor = sha
    try:
        while requests < REQUEST_LIMIT:
            query = urlencode({"sha": cursor, "path": path, "per_page": 1})
            rows = get("/commits?" + query).json()
            if not isinstance(rows, list) or len(rows) != 1:
                break
            commit = _sha(rows[0]["sha"])
            info = get("/commits/" + commit).json()
            if info["sha"] != commit or len(info["parents"]) > 1:
                break
            file = next((f for f in info["files"] if f["filename"] == path), None)
            if not file or file["status"] not in {"modified", "added"}:
                break
            current = declaration_at(commit)
            if not current or _version(current) != _version(selected):
                break
            date = (
                datetime.fromisoformat(info["commit"]["committer"]["date"])
                .date()
                .isoformat()
            )
            parent = _sha(info["parents"][0]["sha"]) if info["parents"] else ""
            if file["status"] == "added":
                return Attribution(
                    "Declaration introduced in this file",
                    commit,
                    date,
                    current.span.start,
                    current.source,
                )
            if not parent:
                break
            prior = declaration_at(parent)
            if prior is None or _version(prior) != _version(current):
                description = (
                    "Declared version changed"
                    if prior
                    else "Declaration introduced in this file"
                )
                return Attribution(
                    description, commit, date, current.span.start, current.source
                )
            cursor = parent
    except GitHubError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        pass
    return Attribution(
        "Unknown. The bounded history does not verify a change to this declaration."
    )


def dependency_history(
    snapshot: SourceSnapshot,
    artifacts: Artifacts,
    client: httpx2.Client,
    repo: str,
    sha: str,
    selected: Declaration,
) -> Attribution:
    selection = [
        selected.name,
        selected.group,
        selected.span.path,
        selected.span.start,
        selected.source,
    ]
    key = hashlib.sha256(json.dumps(selection).encode()).hexdigest()
    try:
        return artifacts.read(
            snapshot.database.parent,
            "history:" + key,
            "2" if selected.name.startswith("go:") else "1",
            TypeAdapter(Attribution),
            lambda: _trace(client, repo, sha, selected),
        )
    except GitHubError as exc:
        return Attribution(f"Unknown. {exc}")
