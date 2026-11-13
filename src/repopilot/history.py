import hashlib
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlencode

import httpx2
from filelock import FileLock, Timeout
from pydantic import BaseModel, Field

from repopilot.github import GitHubError, api_get, repository_name

PAGE_LIMIT = 2
PAGE_SIZE = 20
DETAIL_LIMIT = 6


class HistoryRecord(BaseModel):
    kind: Literal["issue", "comment", "pull", "commit"]
    id: str
    number: int | None = None
    title: str
    body: str
    author: str | None
    timestamp: str
    updated_at: str
    url: str
    state: str = ""
    paths: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.number or self.id}"


class HistoryLink(BaseModel, frozen=True):
    source: str
    relation: Literal["comment_on", "references", "merged_as", "closed_by_commit"]
    target: str


class HistoryPage(BaseModel):
    records: list[HistoryRecord] = Field(default_factory=list)
    links: list[HistoryLink] = Field(default_factory=list)
    paths: tuple[str, ...] = ()
    more: bool = False


class Observation(BaseModel):
    repository: str
    sha: str
    issue: int | None
    observed_at: float
    records: list[HistoryRecord]
    links: list[HistoryLink]


def _sha(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("Expected a full commit SHA")
    return value


def _url(actual: str, expected: str) -> str:
    if actual.casefold() != expected.casefold():
        raise ValueError("Unexpected source repository or identifier")
    return expected


class HistorySource:
    def __init__(
        self, repo: str, client: httpx2.Client, db: sqlite3.Connection
    ) -> None:
        self.repo = repo
        self.api = f"https://api.github.com/repos/{repo}"
        self.web = f"https://github.com/{repo}"
        self.db = db
        self.client = client
        self.pending: dict[str, tuple[str, str]] = {}

    def page(
        self,
        endpoint: str,
        parse: Callable[[Any], HistoryPage],
        *,
        immutable: bool = False,
    ) -> HistoryPage:
        url = self.api + endpoint
        cached = self.db.execute(
            "SELECT etag,payload FROM history_pages WHERE url=?", (url,)
        ).fetchone()
        if cached and immutable:
            return HistoryPage.model_validate_json(cached[1])
        response = api_get(self.client, url, etag=cached[0] if cached else "")
        if response.status_code == 304 and cached:
            return HistoryPage.model_validate_json(cached[1])
        try:
            page = parse(response.json())
            page.more = "next" in response.links
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise GitHubError("GitHub returned invalid historical evidence") from exc
        self.pending[url] = (response.headers.get("etag", ""), page.model_dump_json())
        return page

    def pages(
        self, endpoint: str, params: dict[str, str], parse: Callable[[Any], HistoryPage]
    ) -> Iterator[HistoryPage]:
        for index in range(1, PAGE_LIMIT + 1):
            query = urlencode({**params, "per_page": PAGE_SIZE, "page": index})
            page = self.page(f"{endpoint}?{query}", parse)
            yield page
            if not page.more:
                break

    def issue_record(
        self, data: dict[str, Any], *, pull: bool = False
    ) -> HistoryRecord:
        number = int(data["number"])
        if number < 1 or int(data["id"]) < 1:
            raise ValueError
        url = _url(
            data["html_url"], f"{self.web}/{'pull' if pull else 'issues'}/{number}"
        )
        return HistoryRecord(
            kind="pull" if pull else "issue",
            id=str(data["id"]),
            number=number,
            title=data["title"][:512],
            body=(data.get("body") or "")[:4000],
            author=(data.get("user") or {}).get("login"),
            timestamp=data["created_at"],
            updated_at=data["updated_at"],
            url=url,
            state="merged" if pull and data.get("merged_at") else data["state"],
            labels=tuple(label["name"] for label in data.get("labels", [])[:40]),
        )

    def closed(self, rows: list[dict[str, Any]]) -> HistoryPage:
        return HistoryPage(
            records=[
                self.issue_record(row)
                for row in rows[:PAGE_SIZE]
                if row["state"] == "closed" and "pull_request" not in row
            ]
        )

    def commit(self, data: dict[str, Any]) -> HistoryRecord:
        sha = _sha(data["sha"])
        commit = data["commit"]
        message = commit["message"][:4000]
        author = commit.get("author") or commit["committer"]
        return HistoryRecord(
            kind="commit",
            id=sha,
            title=message.partition("\n")[0][:512],
            body=message,
            author=(data.get("author") or {}).get("login") or author.get("name"),
            timestamp=author["date"],
            updated_at=commit["committer"]["date"],
            url=_url(data["html_url"], f"{self.web}/commit/{sha}"),
            paths=tuple(f["filename"] for f in data.get("files", [])[:60]),
        )

    def single_commit(self, sha: str) -> HistoryPage:
        def parse(row: dict[str, Any]) -> HistoryPage:
            record = self.commit(row)
            if record.id != sha:
                raise ValueError
            return HistoryPage(records=[record])

        return self.page(f"/commits/{sha}?per_page=60", parse, immutable=True)

    def discussion(self, issue: int, rows: list[dict[str, Any]]) -> HistoryPage:
        page = HistoryPage()
        for row in rows[:PAGE_SIZE]:
            event = row["event"]
            if event == "commented":
                comment_id = int(row["id"])
                record = HistoryRecord(
                    kind="comment",
                    id=str(comment_id),
                    title=f"Comment on #{issue}",
                    body=(row.get("body") or "")[:4000],
                    author=(row.get("user") or {}).get("login"),
                    timestamp=row["created_at"],
                    updated_at=row["updated_at"],
                    url=_url(
                        row["html_url"],
                        f"{self.web}/issues/{issue}#issuecomment-{comment_id}",
                    ),
                )
                page.records.append(record)
                page.links.append(
                    HistoryLink(
                        source=record.key,
                        relation="comment_on",
                        target=f"issue:{issue}",
                    )
                )
            elif event == "cross-referenced":
                source = (row.get("source") or {}).get("issue") or {}
                number = source.get("number")
                if (
                    source.get("pull_request")
                    and isinstance(number, int)
                    and number > 0
                    and str(source.get("html_url")).casefold()
                    == f"{self.web}/pull/{number}".casefold()
                ):
                    page.links.append(
                        HistoryLink(
                            source=f"pull:{number}",
                            relation="references",
                            target=f"issue:{issue}",
                        )
                    )
            elif event in {"closed", "referenced"} and row.get("commit_id"):
                sha = _sha(row["commit_id"])
                if (
                    str(row.get("commit_url")).casefold()
                    == f"{self.api}/commits/{sha}".casefold()
                ):
                    page.links.append(
                        HistoryLink(
                            source=f"issue:{issue}",
                            relation="closed_by_commit"
                            if event == "closed"
                            else "references",
                            target=f"commit:{sha}",
                        )
                    )
        return page

    def pull(self, number: int) -> HistoryPage:
        def parse(data: dict[str, Any]) -> HistoryPage:
            record = self.issue_record(data, pull=True)
            if record.number != number:
                raise ValueError
            page = HistoryPage(records=[record])
            if data.get("merged_at") and data.get("merge_commit_sha"):
                page.links.append(
                    HistoryLink(
                        source=record.key,
                        relation="merged_as",
                        target="commit:" + _sha(data["merge_commit_sha"]),
                    )
                )
            return page

        page = self.page(f"/pulls/{number}", parse)
        files = self.page(
            f"/pulls/{number}/files?per_page=60",
            lambda rows: HistoryPage(paths=tuple(f["filename"] for f in rows[:60])),
        )
        page.records[0].paths = files.paths
        return page

    def collect(
        self, sha: str, issue: int | None
    ) -> tuple[list[HistoryRecord], list[HistoryLink]]:
        records: dict[str, HistoryRecord] = {}
        links: dict[HistoryLink, None] = {}

        def add(page: HistoryPage) -> None:
            records.update((r.key, r) for r in page.records)
            links.update(dict.fromkeys(page.links))

        if issue is None:
            for page in self.pages(
                "/issues",
                {"state": "closed", "sort": "updated", "direction": "desc"},
                self.closed,
            ):
                add(page)
            for page in self.pages(
                "/commits",
                {"sha": sha},
                lambda rows: HistoryPage(
                    records=[self.commit(row) for row in rows[:PAGE_SIZE]]
                ),
            ):
                add(page)
            commits = [r.id for r in records.values() if r.kind == "commit"]
        else:

            def selected(data: dict[str, Any]) -> HistoryPage:
                record = self.issue_record(data)
                if record.number != issue:
                    raise ValueError
                return HistoryPage(records=[record])

            add(self.page(f"/issues/{issue}", selected))
            for page in self.pages(
                f"/issues/{issue}/timeline",
                {},
                lambda rows: self.discussion(issue, rows),
            ):
                add(page)
            pulls = [
                int(link.source.split(":")[1])
                for link in links
                if link.source.startswith("pull:")
            ]
            for number in dict.fromkeys(pulls[:DETAIL_LIMIT]):
                add(self.pull(number))
            commits = [
                link.target.split(":")[1]
                for link in links
                if link.target.startswith("commit:")
            ]
        for commit in list(dict.fromkeys(commits))[:DETAIL_LIMIT]:
            add(self.single_commit(commit))
        return list(records.values()), list(links)


class History:
    def __init__(
        self,
        directory: Path,
        client: httpx2.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = directory / "history.db"
        self.client = client
        self.clock = clock
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS history_pages(url TEXT PRIMARY KEY,etag TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(repo TEXT,sha TEXT,issue INTEGER,payload TEXT NOT NULL,PRIMARY KEY(repo,sha,issue));
            """)

    def observations(self, name: str, sha: str) -> list[Observation]:
        with closing(sqlite3.connect(self.path)) as db:
            return [
                Observation.model_validate_json(row[0])
                for row in db.execute(
                    "SELECT payload FROM observations WHERE repo=? AND sha=? ORDER BY issue",
                    (name.lower(), sha),
                )
            ]

    def read(self, name: str, sha: str, issue: int | None = None) -> Observation | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT payload FROM observations WHERE repo=? AND sha=? AND issue=?",
                (name.lower(), sha, issue or 0),
            ).fetchone()
        return Observation.model_validate_json(row[0]) if row else None

    def load(
        self, name: str, sha: str, issue: int | None = None, *, refresh: bool = False
    ) -> Observation:
        repo = repository_name(f"https://github.com/{name}").lower()
        _sha(sha)
        if issue is not None and issue <= 0:
            raise ValueError("Expected a positive issue number")
        lock = self.path.parent / (
            "history-" + hashlib.sha256(repo.encode()).hexdigest() + ".lock"
        )
        try:
            with (
                FileLock(lock, timeout=0),
                closing(sqlite3.connect(self.path)) as db,
                db,
            ):
                if not refresh and (saved := self.read(repo, sha, issue)):
                    return saved
                source = HistorySource(repo, self.client, db)
                records, links = source.collect(sha, issue)
                observed = Observation(
                    repository=repo,
                    sha=sha,
                    issue=issue,
                    observed_at=self.clock(),
                    records=records,
                    links=links,
                )
                db.executemany(
                    "INSERT OR REPLACE INTO history_pages VALUES (?,?,?)",
                    ((url, *data) for url, data in source.pending.items()),
                )
                db.execute(
                    "INSERT OR REPLACE INTO observations VALUES (?,?,?,?)",
                    (repo, sha, issue or 0, observed.model_dump_json()),
                )
                return observed
        except Timeout as exc:
            raise GitHubError(
                "A history request is already running for this repository"
            ) from exc
