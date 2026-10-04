"""A failed verifier must still reach Receipt creation under Actions' bash -e."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


def workflow_bash() -> str | None:
    """Use Git Bash on Windows, never the WSL launcher found first on PATH."""
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            parent = Path(git).parent
            for path in (parent / "bash.exe", parent.parent / "bin" / "bash.exe", parent.parent.parent / "bin" / "bash.exe"):
                if path.is_file():
                    return str(path)
        return None
    return shutil.which("bash")


BASH = workflow_bash()
SHELL_REQUIRED = os.environ.get("GITHUB_ACTIONS") == "true"


@pytest.mark.skipif(not SHELL_REQUIRED and (BASH is None or not shutil.which("jq")), reason="requires workflow Bash and jq")
def test_failed_check_is_recorded_and_next_check_runs():
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    match = re.search(r"  run_check\(\) \{.*?\n  \}", script, re.S)
    assert match is not None
    command = "set -euo pipefail\nchecks='[]'\nfailed=0\n" + match.group(0) + '''
run_check "failed test" "test" false
run_check "passed test" "test" true
[[ "$failed" == 1 ]]
echo "$checks"
'''
    assert BASH is not None, "CI requires Git Bash; workflow shell checks must execute"
    result = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True, check=True)
    checks = json.loads(result.stdout.splitlines()[-1])
    assert [(c["status"], c["exit_code"]) for c in checks] == [("failed", 1), ("passed", 0)]


@pytest.mark.skipif(not SHELL_REQUIRED and (BASH is None or not shutil.which("jq")), reason="requires workflow Bash and jq")
@pytest.mark.parametrize("case", ["created", "duplicate", "existing", "wrong_id", "wrong_error", "unauthorized", "server_error", "malformed", "bad_success", "transport"])
def test_delivery_retains_existing_receipt_and_rejects_other_errors(case):
    import hashlib
    import shlex

    sha = "a" * 40
    expected = "rcpt_" + hashlib.sha256(f"github-cloud:\0{42}\0{sha}".encode()).hexdigest()[:32]
    conflict = {"error": {"code": "receipt_conflict", "message": f'A different receipt with receipt_id "{expected}" already exists in this organisation.'}}
    body = {"receipt_id": expected}
    http = "201"
    curl_exit = 0
    if case == "duplicate":
        http = "200"
    elif case in ("existing", "wrong_id", "wrong_error"):
        http, body = "409", conflict
        if case == "wrong_id":
            body["error"]["message"] = "A different Receipt already exists."
        if case == "wrong_error":
            body["error"]["code"] = "verification_evidence_conflict"
    elif case == "unauthorized":
        http = "401"
    elif case == "server_error":
        http = "500"
    elif case == "bad_success":
        body["receipt_id"] = "rcpt_wrong"
    elif case == "transport":
        curl_exit = 7
    serialized = "not JSON" if case == "malformed" else json.dumps(body)
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    function = re.search(r"^deliver_receipt\(\) \{.*?^\}", script, re.S | re.M)
    assert function is not None
    command = "set -euo pipefail\n" + function.group(0) + f'''
GITHUB_REPOSITORY_ID=42
sha={sha}
OPENSHARD_API_BASE=https://example.invalid
payload='{{}}'
oidc_token=not-a-real-token
verification_status=failed
overall_failed=1
curl() {{
  while [[ "$1" != -o ]]; do shift; done
  printf '%s' {shlex.quote(serialized)} > "$2"
  printf '%s' {http}
  return {curl_exit}
}}
result=0
deliver_receipt || result=$?
printf '%s %s %s' "$result" "$overall_failed" "$verification_status"
'''
    assert BASH is not None, "CI requires Git Bash; workflow shell checks must execute"
    result = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True, check=True)
    expected_exit = 0 if case in ("created", "duplicate", "existing") else 1
    assert result.stdout.splitlines()[-1] == f"{expected_exit} 1 failed"


@pytest.mark.skipif(not SHELL_REQUIRED and BASH is None, reason="requires workflow Bash")
@pytest.mark.parametrize("exists,fail_action", [(False, ""), (True, ""), (False, "create"), (True, "upload"), (True, "edit")])
def test_release_finishes_existing_page_and_propagates_command_errors(exists, fail_action, tmp_path):
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/release.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["github-release"]["steps"] if s.get("name") == "Create release and attach artifacts")
    import shlex

    command = f'''set -euo pipefail
TAG=v0.4.10
calls={shlex.quote(tmp_path.joinpath('calls').as_posix())}
gh() {{
  printf '%s\\n' "$2" >> "$calls"
  if [[ "$2" == view ]]; then return {0 if exists else 1}; fi
  if [[ "$2" == {shlex.quote(fail_action)} ]]; then return 5; fi
}}
''' + script
    assert BASH is not None, "CI requires workflow Bash"
    result = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True)
    assert result.returncode == (5 if fail_action else 0), result.stderr
    expected = ["view", "upload", "edit"] if exists else ["view", "create"]
    if fail_action == "upload":
        expected = expected[:2]
    assert tmp_path.joinpath("calls").read_text().splitlines() == expected
