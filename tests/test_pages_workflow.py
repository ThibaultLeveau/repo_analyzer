import re
from pathlib import Path

import yaml


def test_only_default_branch_build_can_deploy() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.load(
        (root / ".github/workflows/pages.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"push", "pull_request"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert workflow["permissions"] == {"contents": "read"}
    build, deploy = workflow["jobs"]["build"], workflow["jobs"]["deploy"]
    assert "permissions" not in build and "if" not in build
    assert any(
        s.get("run") == "python -m mkdocs build --strict" for s in build["steps"]
    )
    upload = next(
        s for s in build["steps"] if "upload-pages-artifact" in s.get("uses", "")
    )
    assert (
        upload["with"]["path"]
        == yaml.safe_load((root / "mkdocs.yml").read_text())["site_dir"]
    )
    assert (
        upload["if"]
        == deploy["if"]
        == "github.event_name == 'push' && github.ref_name == github.event.repository.default_branch"
    )
    assert deploy["needs"] == "build"
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}
    assert deploy["environment"]["name"] == "github-pages"
    assert len(deploy["steps"]) == 1 and deploy["steps"][0]["uses"].startswith(
        "actions/deploy-pages@"
    )
    for step in build["steps"] + deploy["steps"]:
        if "uses" in step:
            assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"])
