from dataclasses import replace
from pathlib import Path

import httpx2
import pytest
from test_manifests import manifest_snapshot

from repopilot.dependencies import Dependency, analyze_dependencies
from repopilot.doc_fetch import MAX_BYTES
from repopilot.official_docs import DEFAULTS, FRESH, OfficialDocs, excerpts, source_for
from repopilot.search import LexicalSearch

HTML = """<html><title>Publisher manual</title><main><h1>Guide</h1>
<section id="install"><h2>Install</h2><p>Install the dependency.</p></section>
<h2 id="parsing">Parsing</h2><p>Call the decoder for a frobnicator.</p>
<script>private tracker</script></main></html>"""
REPO, SHA = "example/catalog", "a" * 40


def reply(
    code: int, body: str = "", media: str = "text/html", **headers: str
) -> httpx2.Response:
    return httpx2.Response(
        code,
        headers={"content-type": media, **headers},
        stream=httpx2.ByteStream(body.encode()),
    )


class Publisher:
    def __init__(self, directory: Path) -> None:
        self.paths: list[str] = []
        self.now = 1000.0
        self.robots = ""
        self.destination = ""
        self.status = 200
        self.body = HTML
        self.ips = ["8.8.8.8"]
        self.docs = OfficialDocs(
            directory,
            lambda: self.now,
            transport=httpx2.MockTransport(self.respond),
            resolve=lambda _: self.ips,
        )

    def respond(self, request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == self.ips[0]
        assert request.headers["Host"] == request.extensions["sni_hostname"]
        assert request.headers["User-Agent"] == "RepoPilot"
        self.paths.append(request.url.path)
        if request.url.path == "/robots.txt":
            return reply(200, self.robots, "text/plain")
        if self.destination and request.url.path != self.destination:
            return reply(302, location=self.destination)
        return reply(self.status, self.body)


def test_selected_page_is_versioned_searchable_and_isolated(tmp_path: Path) -> None:
    snapshot = manifest_snapshot(
        tmp_path / "sources", {"requirements.txt": "Django==5.2.4"}
    )
    package = analyze_dependencies(snapshot).packages["django"]
    (tmp_path / "official-docs.toml").write_text(
        '[sources.django]\nurl="https://publisher.example/{series}/install"\nedition="{series}"',
        encoding="utf-8",
    )
    remote = Publisher(tmp_path)
    remote.destination = "/5.2/install/"
    source = source_for(package, remote.docs.catalog)
    assert source and source.url == "https://publisher.example/5.2/install"
    remote.docs.index(REPO, SHA, package)
    assert remote.paths == ["/robots.txt", "/5.2/install", "/5.2/install/"]
    doc = remote.docs.read(REPO.upper(), SHA, "django", source)
    assert doc and not doc.error and not doc.stale
    assert (doc.edition, doc.version, doc.fetched) == ("5.2", "==5.2.4", remote.now)
    assert [x.url for x in doc.chunks] == [
        source.url + "/#install",
        source.url + "/#parsing",
    ]
    assert doc.chunks[0].title == "Publisher manual"
    again = OfficialDocs(
        tmp_path, lambda: remote.now, resolve=lambda _: pytest.fail("unexpected DNS")
    )
    again.index(REPO, SHA, package)
    hits = again.search(REPO, SHA, "frobnicator")
    assert [hit[-1] for hit in hits] == [doc.chunks[1]]
    assert not again.search(REPO, "b" * 40, "frobnicator")
    assert not again.search("other/catalog", SHA, "frobnicator")
    assert not again.search(REPO, SHA, 'tracker" OR')
    assert not LexicalSearch(snapshot).search("frobnicator")
    remote.now += FRESH
    remote.status = 503
    remote.docs.index(REPO, SHA, package)
    cached = remote.docs.read(REPO, SHA, "django", source)
    assert (
        cached
        and cached.stale
        and "503" in cached.error
        and cached.chunks == doc.chunks
    )
    assert not remote.docs.search(REPO, SHA, "frobnicator")
    remote.status = 200
    remote.body = HTML.replace("frobnicator", "successor")
    remote.docs.index(REPO, SHA, package)
    assert not remote.docs.search(REPO, SHA, "frobnicator")
    assert remote.docs.search(REPO, SHA, "successor")
    mixed = replace(
        package,
        declarations=package.declarations
        + (replace(package.declarations[0], constraint=">=4"),),
    )
    assert (
        fallback := source_for(mixed, remote.docs.catalog)
    ) and fallback.edition == "stable"


@pytest.mark.parametrize(
    ("scenario", "message", "requests"),
    [
        ("private", "public network", 0),
        ("foreign", "approved HTTPS", 2),
        ("blocked", "disallow", 2),
        ("large", "limits", 2),
        ("failure", "503", 2),
        ("denied", "disallow", 1),
        ("rate", "later retry", 2),
    ],
)
def test_destination_limits_and_fetch_failures(
    tmp_path: Path, scenario: str, message: str, requests: int
) -> None:
    remote = Publisher(tmp_path)
    package = Dependency("pyyaml", (), ())
    if scenario == "private":
        remote.ips.append("127.0.0.1")
    elif scenario == "foreign":
        remote.destination = "https://outside.example/page"
    elif scenario == "blocked":
        remote.destination = "/restricted/page"
        remote.robots = "User-agent: *\nDisallow: /restr*/"
    elif scenario == "denied":
        remote.robots = "User-agent: RepoPilot\nDisallow: /"
    elif scenario == "large":
        remote.body = "x" * (MAX_BYTES + 1)
    else:
        remote.status = 429 if scenario == "rate" else 503
    remote.docs.index(REPO, SHA, package)
    page = remote.docs.read(REPO, SHA, package.name, DEFAULTS[package.name])
    assert page and page.fetched is None and not page.chunks and message in page.error
    assert len(remote.paths) == requests
    if scenario == "rate":
        remote.docs.index(REPO, SHA, package)
        assert len(remote.paths) == requests


def test_crawl_delay_persists_across_redirect_and_retry(tmp_path: Path) -> None:
    remote = Publisher(tmp_path)
    remote.robots = "User-agent: *\nCrawl-delay: 2"
    remote.destination = "/new"
    package = Dependency("pyyaml", (), ())
    for path in ["/robots.txt", "/wiki/PyYAMLDocumentation", "/new"]:
        before = len(remote.paths)
        remote.docs.index(REPO, SHA, package)
        assert remote.paths[before:] == [path]
        remote.now += 2
    assert remote.docs.search(REPO, SHA, "frobnicator")


def test_links_preserve_the_observed_edition(tmp_path: Path) -> None:
    url = "https://docs.example/5.2/topic?utm=local"
    for canonical, expected in [
        ("/5.2/topic/", "https://docs.example/5.2/topic/"),
        ("/6.0/topic", url),
        ("https://other.example/topic", url),
    ]:
        html = f'<link rel="canonical" href="{canonical}">' + HTML
        assert excerpts(url, html, "text/html")[0].url == expected + "#install"
    snapshot = manifest_snapshot(
        tmp_path / "sources", {"requirements.txt": "Django==5.2.*"}
    )
    dep = analyze_dependencies(snapshot).packages["django"]
    publisher = Publisher(tmp_path)
    publisher.destination = "/en/6.0/topics/install/"
    publisher.docs.index(REPO, SHA, dep)
    source = source_for(dep, publisher.docs.catalog)
    assert source
    page = publisher.docs.read(REPO, SHA, dep.name, source)
    assert page and page.error and not page.chunks
