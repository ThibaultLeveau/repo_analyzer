import re
import sqlite3
import time
import tomllib
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx2
from bs4 import BeautifulSoup, Tag
from filelock import FileLock, Timeout

from repopilot.dependencies import Dependency
from repopilot.doc_fetch import DocFetcher, DocsUnavailable, addresses, approved
from repopilot.search import CHUNK_CHARS

MAX_CHUNKS = 128
FRESH = 7 * 86_400


@dataclass(frozen=True)
class DocSource:
    url: str
    edition: str


DEFAULTS = {
    "pyyaml": DocSource("https://pyyaml.org/wiki/PyYAMLDocumentation", "Unversioned"),
    "requests": DocSource(
        "https://requests.readthedocs.io/en/latest/user/quickstart/", "Latest"
    ),
    "django": DocSource(
        "https://docs.djangoproject.com/en/{series}/topics/install/", "{series}"
    ),
}


def source_for(package: Dependency, catalog: dict[str, DocSource]) -> DocSource | None:
    source = catalog.get(package.name)
    if not source:
        return None
    versions = {item.constraint for item in package.declarations}
    pin = (
        re.fullmatch(r"==(\d+\.\d+)(?:\.\d+|\.\*)?", next(iter(versions), ""))
        if len(versions) == 1
        else None
    )
    series = pin.group(1) if pin else "stable"
    return DocSource(
        source.url.replace("{series}", series),
        source.edition.replace("{series}", series),
    )


@dataclass(frozen=True)
class Excerpt:
    title: str
    section: str
    url: str
    text: str


def excerpts(url: str, body: str, media: str) -> list[Excerpt]:
    chunks: list[Excerpt] = []
    title = section = ""
    link = url

    def append(text: str) -> None:
        for start in range(0, len(text), CHUNK_CHARS):
            if len(chunks) == MAX_CHUNKS:
                break
            part = text[start : start + CHUNK_CHARS]
            if chunks and chunks[-1].section == section and chunks[-1].url == link:
                prev = chunks[-1]
                if len(prev.text) + 1 + len(part) <= CHUNK_CHARS:
                    chunks[-1] = Excerpt(title, section, link, prev.text + "\n" + part)
                    continue
            chunks.append(Excerpt(title, section, link, part))

    if media.startswith("text/plain"):
        title = section = url.rsplit("/", 1)[-1] or "Documentation"
        append(body.strip())
        return chunks

    soup = BeautifulSoup(body, "html.parser")
    canonical = soup.select_one("link[rel~=canonical][href]")
    if canonical:
        observed = httpx2.URL(url)
        try:
            alias = approved(
                str(observed.join(str(canonical.get("href")))), observed.host
            )
        except (DocsUnavailable, httpx2.InvalidURL):
            alias = observed
        # A publisher can designate its newest edition as canonical for older pages.
        if alias.path.rstrip("/") == observed.path.rstrip("/") and (
            not alias.query or alias.query == observed.query
        ):
            link = url = str(alias)
    for tag in soup.select("nav,aside,script,style,footer,[hidden],.headerlink"):
        tag.decompose()
    root = soup.select_one("main,article,[role=main]") or soup.body or soup
    first = root.find("h1")
    title = (soup.title.get_text(" ", strip=True) if soup.title else "") or (
        first.get_text(" ", strip=True) if first else url
    )
    section = title
    for tag in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "pre", "li"]):
        if len(chunks) >= MAX_CHUNKS:
            break
        if tag.find_parent(["p", "pre", "li"]):
            continue
        content = tag.get_text(
            "" if tag.name == "pre" else " ", strip=tag.name != "pre"
        ).strip()
        if tag.name.startswith("h"):
            section = content or title
            parent = tag.find_parent("section", id=True)
            anchor = tag.get("id") or (
                parent.get("id") if isinstance(parent, Tag) else None
            )
            link = url + ("#" + quote(str(anchor), safe="") if anchor else "")
        elif content:
            append(content)
    return chunks


@dataclass(frozen=True)
class DocPage:
    package: str
    version: str
    edition: str
    fetched: float | None
    stale: bool
    error: str
    chunks: list[Excerpt]


class OfficialDocs:
    def __init__(
        self,
        directory: Path,
        clock: Callable[[], float] = time.time,
        *,
        transport: httpx2.BaseTransport | None = None,
        resolve: Callable[[str], list[str]] = addresses,
    ) -> None:
        self.path = directory / "official-docs.db"
        self.clock, self.transport, self.resolve = clock, transport, resolve
        self.catalog = dict(DEFAULTS)
        config = directory / "official-docs.toml"
        if config.is_file():
            with config.open("rb") as file:
                for name, spec in tomllib.load(file).get("sources", {}).items():
                    source = DocSource(**spec)
                    approved(source.url.replace("{series}", "stable"))
                    self.catalog[name] = source
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pages(
                    id INTEGER PRIMARY KEY, repo TEXT, sha TEXT, package TEXT, source TEXT,
                    version TEXT, edition TEXT, fetched REAL, error TEXT NOT NULL DEFAULT '',
                    UNIQUE(repo,sha,package,source));
                CREATE VIRTUAL TABLE IF NOT EXISTS sections USING fts5(
                    page UNINDEXED, title, section, url UNINDEXED, text);
                CREATE TABLE IF NOT EXISTS robots(host TEXT PRIMARY KEY,fetched REAL,body TEXT);
                CREATE TABLE IF NOT EXISTS cooldown(host TEXT PRIMARY KEY,until REAL);
                CREATE TABLE IF NOT EXISTS redirects(url TEXT PRIMARY KEY,target TEXT,fetched REAL);
            """)

    def read(
        self, repo: str, sha: str, package: str, source: DocSource
    ) -> DocPage | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                """SELECT id,version,edition,fetched,error FROM pages
                   WHERE repo=? AND sha=? AND package=? AND source=?""",
                (repo.lower(), sha, package, source.url),
            ).fetchone()
            if not row:
                return None
            key, version, edition, fetched, error = row
            chunks = [
                Excerpt(*item)
                for item in db.execute(
                    "SELECT title,section,url,text FROM sections WHERE page=? ORDER BY rowid",
                    (key,),
                )
            ]
        stale = fetched is not None and (bool(error) or self.clock() - fetched >= FRESH)
        return DocPage(package, version, edition, fetched, stale, error, chunks)

    def search(
        self, repo: str, sha: str, query: str, limit: int = 6
    ) -> list[tuple[str, str, str, float, Excerpt]]:
        terms = list(dict.fromkeys(re.findall(r"\w+", query[:500])))[:24]
        if not terms:
            return []
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute(
                """SELECT package,version,edition,fetched,title,section,url,text FROM sections
                   JOIN pages ON pages.id=sections.page WHERE sections MATCH ?
                   AND repo=? AND sha=? AND error='' AND fetched>?
                   ORDER BY rank,sections.rowid LIMIT ?""",
                (
                    " OR ".join(f'"{term}"' for term in terms),
                    repo.lower(),
                    sha,
                    self.clock() - FRESH,
                    max(0, min(limit, MAX_CHUNKS)),
                ),
            )
            return [
                (pkg, ver, ed, fetched, Excerpt(*part))
                for pkg, ver, ed, fetched, *part in rows
            ]

    def index(self, repo: str, sha: str, package: Dependency) -> None:
        source = source_for(package, self.catalog)
        if source is None:
            raise DocsUnavailable("No official documentation source is configured.")
        version = (
            "; ".join(
                dict.fromkeys(
                    d.constraint for d in package.declarations if d.constraint
                )
            )
            or "Unspecified"
        )
        try:
            with (
                FileLock(self.path.with_suffix(".lock"), timeout=0),
                closing(sqlite3.connect(self.path)) as db,
                db,
            ):
                cached = self.read(repo, sha, package.name, source)
                if cached and cached.fetched is not None and not cached.stale:
                    return
                key = db.execute(
                    """INSERT INTO pages(repo,sha,package,source,version,edition) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(repo,sha,package,source) DO UPDATE SET edition=excluded.edition
                       RETURNING id""",
                    (
                        repo.lower(),
                        sha,
                        package.name,
                        source.url,
                        version,
                        source.edition,
                    ),
                ).fetchone()[0]
                try:
                    with httpx2.Client(
                        transport=self.transport, trust_env=False, timeout=6
                    ) as client:
                        url, page = DocFetcher(
                            client, db, self.clock, self.resolve
                        ).fetch(source.url)
                    if (
                        "{series}" in self.catalog[package.name].url
                        and re.fullmatch(r"\d+\.\d+", source.edition)
                        and source.edition not in httpx2.URL(url).path.split("/")
                        and source.edition in httpx2.URL(source.url).path.split("/")
                    ):
                        raise DocsUnavailable(
                            "The publisher redirected away from the selected edition."
                        )
                    parts = excerpts(
                        url, page.text, page.headers.get("content-type", "")
                    )
                    if not parts:
                        raise DocsUnavailable(
                            "The documentation page has no readable sections."
                        )
                except (
                    ValueError,
                    OSError,
                    httpx2.HTTPError,
                    httpx2.InvalidURL,
                ) as exc:
                    error = (
                        str(exc)
                        if isinstance(exc, DocsUnavailable)
                        else "Could not fetch the official documentation."
                    )
                    db.execute("UPDATE pages SET error=? WHERE id=?", (error, key))
                    return
                db.execute("DELETE FROM sections WHERE page=?", (key,))
                db.executemany(
                    "INSERT INTO sections VALUES(?,?,?,?,?)",
                    (
                        (key, item.title, item.section, item.url, item.text)
                        for item in parts
                    ),
                )
                db.execute(
                    "UPDATE pages SET fetched=?,error='' WHERE id=?",
                    (self.clock(), key),
                )
        except Timeout as exc:
            raise DocsUnavailable(
                "Another documentation request is running. Try again shortly."
            ) from exc
