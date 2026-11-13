import ipaddress
import socket
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx2
from protego import Protego

AGENT = "RepoPilot"
MAX_BYTES = 512_000
ROBOTS_TTL = 86_400


class DocsUnavailable(ValueError):
    pass


def addresses(host: str) -> list[str]:
    return [
        str(address[0])
        for *_, address in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ]


def approved(raw: str, host: str | None = None) -> httpx2.URL:
    url = httpx2.URL(raw)
    if (
        url.scheme != "https"
        or not url.host
        or url.port is not None
        or url.userinfo
        or url.fragment
        or (host and url.host != host)
    ):
        raise DocsUnavailable("Documentation requires an approved HTTPS origin.")
    return url


@dataclass(frozen=True)
class Download:
    code: int
    headers: httpx2.Headers
    text: str


class DocFetcher:
    def __init__(
        self,
        client: httpx2.Client,
        db: sqlite3.Connection,
        clock: Callable[[], float],
        resolve: Callable[[str], list[str]] = addresses,
    ) -> None:
        self.client, self.db, self.clock, self.resolve = client, db, clock, resolve

    def download(self, url: httpx2.URL, *, robots: bool = False) -> Download:
        ips = [ipaddress.ip_address(ip) for ip in self.resolve(url.host)]
        if not ips or any(
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or (
                isinstance(ip, ipaddress.IPv6Address)
                and (ip.ipv4_mapped or ip.sixtofour or ip.teredo)
            )
            for ip in ips
        ):
            raise DocsUnavailable("Documentation requires a public network address.")
        # Connect to the inspected address while verifying TLS against the publisher.
        target = url.copy_with(host=str(ips[0]))
        with self.client.stream(
            "GET",
            target,
            follow_redirects=False,
            headers={
                "Host": url.host,
                "Accept-Encoding": "identity",
                "User-Agent": AGENT,
            },
            extensions={"sni_hostname": url.host},
        ) as response:
            if response.status_code == 429 or (
                response.status_code == 503 and "retry-after" in response.headers
            ):
                retry = response.headers.get("retry-after", "")
                until = self.clock() + 3600
                if retry.isascii() and retry.isdigit() and len(retry) <= 12:
                    until = self.clock() + int(retry)
                else:
                    try:
                        until = parsedate_to_datetime(retry).timestamp()
                    except (ValueError, TypeError, OverflowError):
                        pass
                self.defer(url.host, until)
                raise DocsUnavailable("The documentation site requested a later retry.")
            if response.status_code != 200:
                return Download(response.status_code, response.headers, "")
            media = response.headers.get("content-type", "").split(";")[0].lower()
            if robots and media != "text/plain":
                raise DocsUnavailable("Site access rules could not be read.")
            if not robots and media not in {
                "text/html",
                "application/xhtml+xml",
                "text/plain",
            }:
                raise DocsUnavailable(
                    "The documentation source did not return a text page."
                )
            if (
                response.headers.get("content-encoding", "identity").lower()
                != "identity"
            ):
                raise DocsUnavailable("The site did not return uncompressed text.")
            size = response.headers.get("content-length", "")
            if size.isdigit() and (len(size) > 10 or int(size) > MAX_BYTES):
                raise DocsUnavailable(
                    "The documentation response exceeds the size limit."
                )
            body = bytearray()
            deadline = time.monotonic() + 15
            for block in response.iter_raw():
                if len(body) + len(block) > MAX_BYTES or time.monotonic() > deadline:
                    raise DocsUnavailable(
                        "The documentation response exceeds the fetch limits."
                    )
                body.extend(block)
            return Download(
                response.status_code, response.headers, body.decode("utf-8", "replace")
            )

    def defer(self, host: str, until: float) -> None:
        self.db.execute(
            """INSERT INTO cooldown VALUES (?,?) ON CONFLICT(host)
               DO UPDATE SET until=max(until,excluded.until)""",
            (host, until),
        )

    def check_time(self, host: str) -> None:
        row = self.db.execute(
            "SELECT until FROM cooldown WHERE host=?", (host,)
        ).fetchone()
        if row and self.clock() < row[0]:
            raise DocsUnavailable(
                "The site's crawl interval is active. Try again later."
            )

    def rules(self, url: httpx2.URL) -> Protego:
        row = self.db.execute(
            "SELECT body FROM robots WHERE host=? AND fetched>?",
            (url.host, self.clock() - ROBOTS_TTL),
        ).fetchone()
        if row:
            return Protego.parse(row[0])
        robots = self.download(
            url.copy_with(path="/robots.txt", query=None), robots=True
        )
        if robots.code not in {200, 404, 410}:
            raise DocsUnavailable("Site access rules are unavailable or restricted.")
        self.db.execute(
            "INSERT OR REPLACE INTO robots VALUES (?,?,?)",
            (url.host, self.clock(), robots.text),
        )
        rules = Protego.parse(robots.text)
        self.defer(url.host, self.clock() + self.interval(rules))
        return rules

    @staticmethod
    def interval(rules: Protego) -> float:
        rate = rules.request_rate(AGENT)
        return max(
            rules.crawl_delay(AGENT) or 0, rate.seconds / rate.requests if rate else 0
        )

    def fetch(self, raw: str) -> tuple[str, Download]:
        url = approved(raw)
        self.check_time(url.host)
        rules = self.rules(url)
        for _ in range(4):
            if not rules.can_fetch(str(url), AGENT):
                raise DocsUnavailable("The site's access rules disallow this page.")
            cached = self.db.execute(
                "SELECT target FROM redirects WHERE url=? AND fetched>?",
                (str(url), self.clock() - ROBOTS_TTL),
            ).fetchone()
            if cached:
                url = approved(cached[0], url.host)
                continue
            self.check_time(url.host)
            now = datetime.fromtimestamp(self.clock(), UTC).time()
            for window in (rules.visit_time(AGENT), rules.request_rate(AGENT)):
                if window and window.start_time and window.end_time:
                    start, end = window.start_time, window.end_time
                    allowed = (
                        start <= now <= end
                        if start <= end
                        else (now >= start or now <= end)
                    )
                    if not allowed:
                        raise DocsUnavailable(
                            "The site limits crawling to other hours."
                        )
            self.defer(url.host, self.clock() + self.interval(rules))
            page = self.download(url)
            if page.code == 200:
                return str(url), page
            if page.code not in {301, 302, 303, 307, 308} or not page.headers.get(
                "location"
            ):
                raise DocsUnavailable(f"Documentation unavailable (HTTP {page.code}).")
            target = approved(str(url.join(page.headers["location"])), url.host)
            self.db.execute(
                "INSERT OR REPLACE INTO redirects VALUES (?,?,?)",
                (str(url), str(target), self.clock()),
            )
            url = target
        raise DocsUnavailable("The documentation site redirected too many times.")
