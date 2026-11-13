import os
import sqlite3
import stat
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import astuple, dataclass
from pathlib import Path

import psutil

INTERVAL = 60
HISTORY = 7 * 24 * 60


@dataclass(frozen=True)
class Sample:
    timestamp: float
    cpu: float | None
    rss: int | None
    sqlite: int | None
    chroma: int | None


def _raise(error: OSError) -> None:
    raise error


def disk_bytes(directory: Path, *, chroma: bool) -> int | None:
    root = directory / "chroma" if chroma else directory
    try:
        mode = root.lstat().st_mode
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    if not stat.S_ISDIR(mode) or root.is_junction():
        return None
    try:
        size = 0
        for current, folders, files in os.walk(root, onerror=_raise):
            path = Path(current)
            if path == directory:
                folders[:] = [name for name in folders if name == "sources"]
            folders[:] = [
                name
                for name in folders
                if not (path / name).is_junction() and not (path / name).is_symlink()
            ]
            for name in files:
                if not chroma and not name.endswith(
                    (".db", ".db-wal", ".db-shm", ".db-journal")
                ):
                    continue
                entry = path / name
                if not entry.is_symlink():
                    size += entry.stat().st_size
        return size
    except OSError:
        return None


class ResourceSampler:
    def __init__(self, directory: Path, clock: Callable[[], float]) -> None:
        self.directory = directory
        self.database = directory / "events.db"
        self.clock = clock
        self.process = psutil.Process()
        self.primed = False
        self.lock = threading.Lock()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS samples(timestamp REAL PRIMARY KEY,cpu REAL,rss INTEGER,sqlite INTEGER,chroma INTEGER)"
            )

    def read(self, now: float) -> Sample:
        cpu = None
        try:
            value = self.process.cpu_percent()
            if self.primed:
                cpu = value
            self.primed = True
        except (psutil.Error, OSError):
            self.primed = False
        try:
            rss = self.process.memory_info().rss
        except (psutil.Error, OSError):
            rss = None
        return Sample(
            now,
            cpu,
            rss,
            disk_bytes(self.directory, chroma=False),
            disk_bytes(self.directory, chroma=True),
        )

    def capture(self) -> None:
        with self.lock, closing(sqlite3.connect(self.database)) as db, db:
            now = self.clock()
            last = db.execute("SELECT MAX(timestamp) FROM samples").fetchone()[0]
            if last is not None and 0 <= now - last < INTERVAL:
                return
            sample = self.read(now)
            db.execute(
                "DELETE FROM samples WHERE timestamp <= ? OR timestamp > ?",
                (now - HISTORY * INTERVAL, now),
            )
            db.execute("INSERT INTO samples VALUES (?,?,?,?,?)", astuple(sample))

    def history(self, since: float) -> list[Sample]:
        with closing(sqlite3.connect(self.database)) as db:
            return [
                Sample(*row)
                for row in db.execute(
                    "SELECT * FROM samples WHERE timestamp>=? ORDER BY timestamp DESC LIMIT 60",
                    (since,),
                )
            ]

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.capture()
            except sqlite3.Error:
                pass
            stop.wait(INTERVAL)
