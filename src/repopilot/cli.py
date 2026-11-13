import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep

from filelock import Timeout

from repopilot.architecture import Architecture
from repopilot.events import EventStore, measure
from repopilot.github import GitHubError, repository_name
from repopilot.guide import Guides
from repopilot.imports import import_service
from repopilot.reports import MEDIA_TYPES, Report, build_report
from repopilot.scoring import Scores
from repopilot.snapshots import SnapshotCache, SnapshotError
from repopilot.state import StateStore


def _sha(text: str) -> str:
    if not re.fullmatch("[0-9a-fA-F]{40}", text):
        raise argparse.ArgumentTypeError("expected a full 40-character commit SHA")
    return text.lower()


def _analyze(args: argparse.Namespace, directory: Path) -> Report:
    with import_service(directory) as service:
        job = service.submit(args.url, sha=args.sha)
        while job.status in {"queued", "running"}:
            sleep(0.08)
            job = service.store.job(job.id)
        if job.status != "completed" or not job.sha:
            raise GitHubError(job.error or "Import did not complete.")
        head, _ = service.store.source(job.id)
        if head is None:
            raise GitHubError("The import has no source revision.")
        repo, sha = head.full_name, head.sha
        architecture = service.architecture
        with service.events.scope(repo, sha, job.id), measure("cli.analyze"):
            architecture.artifacts.prepare(service.cache.open(repo, sha))
            architecture.audit(repo, sha)
            if args.generate_guide:
                status = Guides(architecture).generate(repo, sha)
                if status != "ok":
                    raise GitHubError(f"Guide generation is unavailable ({status}).")
            return build_report(architecture, Scores(directory), repo, sha)


def _cached(args: argparse.Namespace, directory: Path) -> Report:
    repo = repository_name("https://github.com/" + args.repository)
    cache = SnapshotCache(directory / "sources")
    cache.open(repo, args.sha)
    architecture = Architecture(cache, StateStore(directory / "state.db"))
    with EventStore(directory).scope(repo, args.sha), measure("cli.export"):
        return build_report(architecture, Scores(directory), repo, args.sha)


def _write(path: Path, content: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        temporary.write_bytes(content)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="repopilot", description="Analyze a repository or export a cached report."
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("REPOPILOT_DATA_DIR", Path.home() / ".repopilot")),
    )
    shared.add_argument("--format", choices=MEDIA_TYPES, default="json")
    shared.add_argument(
        "--output",
        type=Path,
        help="Write the report to this file. The default is stdout.",
    )
    actions = parser.add_subparsers(dest="action", required=True)
    analyze = actions.add_parser(
        "analyze",
        parents=[shared],
        help="Import a public repository and analyze its snapshot.",
    )
    analyze.add_argument("url")
    analyze.add_argument(
        "--sha",
        type=_sha,
        help="Use this exact commit instead of the default branch head.",
    )
    analyze.add_argument(
        "--generate-guide",
        action="store_true",
        help="Request cited onboarding notes from the configured model.",
    )
    export = actions.add_parser(
        "export", parents=[shared], help="Export previously saved analysis offline."
    )
    export.add_argument("repository", metavar="OWNER/REPO")
    export.add_argument("--sha", type=_sha, required=True)
    args = parser.parse_args()
    try:
        directory = args.data_dir.expanduser().resolve()
        report = (
            _analyze(args, directory)
            if args.action == "analyze"
            else _cached(args, directory)
        )
        content = report.render(args.format).encode("utf-8")
        if args.output:
            _write(args.output, content)
        else:
            sys.stdout.buffer.write(content)
            sys.stdout.buffer.flush()
    except FileNotFoundError as exc:
        print(f"repopilot: {exc}", file=sys.stderr)
        return 3 if args.action == "export" and not exc.filename else 1
    except Timeout:
        print(
            "repopilot: An import worker already owns this data directory.",
            file=sys.stderr,
        )
        return 1
    except (OSError, sqlite3.Error, SnapshotError, GitHubError) as exc:
        print(f"repopilot: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
