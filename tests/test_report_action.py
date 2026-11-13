import json
import os
import re
import runpy
import shlex
import subprocess
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from journey_fixture import REPO, Catalog
from test_cli import run_cli

WORKFLOW = Path(__file__).parents[1] / "examples/report-action/repopilot-report.yml"


def workflow() -> dict[str, Any]:
    return dict(yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader))


def test_permissions_triggers_and_pins() -> None:
    config = workflow()
    assert set(config["on"]) == {
        "push",
        "pull_request",
        "workflow_dispatch",
        "workflow_call",
    }
    assert config["permissions"] == {"contents": "read"}
    assert config["on"]["workflow_call"]["inputs"]["comment"]["default"] == "false"
    report, comment = config["jobs"]["report"], config["jobs"]["comment"]
    assert "if" not in report and "permissions" not in report
    assert comment["needs"] == "report" and comment["permissions"] == {
        "pull-requests": "write"
    }
    steps = report["steps"]
    for step in steps:
        if "uses" in step:
            assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
    assert re.search(r"repo_analyzer\.git@[0-9a-f]{40}", steps[1]["run"])
    assert steps[3]["with"] == {
        "name": "repopilot-report",
        "path": "repopilot-report.json",
        "if-no-files-found": "error",
        "overwrite": "true",
    }
    assert steps[2]["env"] == {
        "REPO_URL": "https://github.com/${{ github.event.pull_request.head.repo.full_name || github.repository }}",
        "REVISION": "${{ github.event.pull_request.head.sha || github.sha }}",
    }
    assert "secrets." not in WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("event", "head", "opt_in", "var", "allowed"),
    [
        ("push", "o/r", True, "true", False),
        ("pull_request", "fork/r", True, "true", False),
        ("pull_request", "o/r", False, "", False),
        ("pull_request", "o/r", True, "", True),
        ("pull_request", "o/r", False, "true", True),
    ],
)
def test_comment_condition(
    event: str, head: str, opt_in: bool, var: str, allowed: bool
) -> None:
    condition = workflow()["jobs"]["comment"]["if"]
    # Check the comparison operators, ordering and grouping from the actual workflow.
    clauses = re.fullmatch(
        r"github\.event_name == '([^']+)' && github\.event\.pull_request\.head\.repo\.full_name == github\.repository && "
        r"\(inputs\.comment == (true|false) \|\| vars\.REPOPILOT_COMMENT == '([^']+)'\)",
        condition,
    )
    assert clauses
    assert (
        event == clauses[1]
        and head == "o/r"
        and (opt_in == (clauses[2] == "true") or var == clauses[3])
    ) == allowed


def test_comment_rerun_updates_only_the_bot_comment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    step = workflow()["jobs"]["comment"]["steps"][0]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    script = re.search(r"python - <<'PY'\n(.*?)\nPY", step["run"], re.DOTALL)
    assert script
    code = tmp_path / "comment.py"
    code.write_text(textwrap.dedent(script[1]), encoding="utf-8")
    marker = "<!-- repopilot-report -->\n"
    comments = [
        {"id": 12, "user": {"login": "contributor"}, "body": marker + "User text"}
    ]
    methods = []
    for name, value in {
        "GITHUB_REPOSITORY": "owner/repo",
        "PR_NUMBER": "7",
        "GITHUB_RUN_ID": "9",
        "REVISION": "a" * 40,
    }.items():
        monkeypatch.setenv(name, value)

    def read(args: list[str]) -> bytes:
        assert args == [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "repos/owner/repo/issues/7/comments",
        ]
        return json.dumps([comments]).encode()

    def write(args: list[str], *, check: bool) -> None:
        assert check and args[:4] == ["gh", "api", "--silent", "--method"]
        methods.append(args[4])
        body = json.loads(Path(args[-1]).read_text(encoding="utf-8"))["body"]
        assert (
            body.startswith(marker)
            and "/actions/runs/" + os.environ["GITHUB_RUN_ID"] in body
        )
        if args[4] == "POST":
            assert args[5] == "repos/owner/repo/issues/7/comments"
            comments.append(
                {"id": 15, "user": {"login": "github-actions[bot]"}, "body": body}
            )
        else:
            assert args[5] == "repos/owner/repo/issues/comments/15"
            comments[-1]["body"] = body

    with (
        patch.object(subprocess, "check_output", read),
        patch.object(subprocess, "run", write),
    ):
        runpy.run_path(str(code))
        monkeypatch.setenv("GITHUB_RUN_ID", "10")
        runpy.run_path(str(code))
    assert methods == ["POST", "PATCH"] and len(comments) == 2


def test_workflow_runs_its_exact_cli_command(
    cli_env: dict[str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = workflow()["jobs"]["report"]["steps"][2]
    archive = Catalog()
    inputs = {"$REPO_URL": "https://github.com/" + REPO, "$REVISION": archive.sha}
    command = [inputs.get(arg, arg) for arg in shlex.split(step["run"])]
    assert command[0] == "repopilot"
    monkeypatch.chdir(tmp_path)
    result = run_cli(cli_env, *command[1:])
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "repopilot-report.json").read_bytes())
    assert report["sha"] == archive.sha and report["sections"]["Architecture"]
