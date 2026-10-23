"""Recover capture candidates when Actions omits the push commits array."""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_cloud_receipt_failure_capture import BASH, SHELL_REQUIRED


@pytest.mark.skipif(not SHELL_REQUIRED and (BASH is None or not shutil.which("jq")), reason="requires workflow Bash and jq")
@pytest.mark.parametrize("case", ["missing", "empty", "explicit", "new_branch", "missing_before", "unknown_before", "force_push"])
def test_push_commit_discovery(tmp_path, case):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("commit", "--allow-empty", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "first")
    first = git("rev-parse", "HEAD")
    git("commit", "--allow-empty", "-m", "second")
    head = git("rev-parse", "HEAD")
    event = {"before": base}
    expected = [first, head]
    if case == "empty":
        event["commits"] = []
    elif case == "explicit":
        event["commits"] = [{"id": head}]
        expected = [head]
    elif case == "new_branch":
        event["before"] = "0" * 40
        expected = [head]
    elif case == "missing_before":
        event = {}
    elif case == "unknown_before":
        event["before"] = "f" * 40
    elif case == "force_push":
        git("checkout", "-b", "other", base)
        git("commit", "--allow-empty", "-m", "other")
        event["before"] = git("rev-parse", "HEAD")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(event))
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["capture"]["steps"] if step.get("name") == "Verify and create hosted Receipts")
    discovery = script.split("pr_body_for_sha()", 1)[0]
    result = subprocess.run(
        [str(BASH), "-e", "-c", discovery + '\nprintf "%s\\n" "$shas"'],
        cwd=tmp_path, text=True, capture_output=True,
        env={**os.environ, "GITHUB_EVENT_NAME": "push", "GITHUB_EVENT_PATH": str(event_path), "GITHUB_SHA": head},
    )
    if case in {"missing_before", "unknown_before"}:
        assert result.returncode != 0
        assert "Cannot recover" in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == expected
