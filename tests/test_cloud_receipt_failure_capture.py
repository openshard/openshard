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
@pytest.mark.parametrize("case", ["created", "duplicate", "attached", "attached_duplicate", "attached_wrong_sha", "attached_empty", "session_not_corroborated", "session_wrong_sha", "existing", "wrong_id", "wrong_error", "unauthorized", "server_error", "malformed", "bad_success", "transport"])
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
    elif case in ("attached", "attached_duplicate", "attached_wrong_sha", "attached_empty"):
        http = "200" if case == "attached_duplicate" else "201"
        body = {
            "outcome": "verification_attached",
            "head_sha": sha if case != "attached_wrong_sha" else "b" * 40,
            "receipt_id": None,
            "attached": [] if case == "attached_empty" else [
                {"receipt_id": "rcpt_existing", "outcome": "recorded", "state_applied": True}
            ],
        }
    elif case in ("session_not_corroborated", "session_wrong_sha"):
        http = "200"
        body = {
            "outcome": "session_not_corroborated",
            "head_sha": sha if case == "session_not_corroborated" else "b" * 40,
            "receipt_id": None,
        }
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
    expected_exit = 0 if case in ("created", "duplicate", "attached", "attached_duplicate", "existing") else 1
    if case == "session_not_corroborated":
        expected_exit = 3
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


SESSION_TRAILER = "Fix it\n\nClaude-Session: https://claude.ai/code/session_01HSAXkLPtdu65CqEWgJgAUF\n"


@pytest.mark.skipif(not SHELL_REQUIRED and BASH is None, reason="requires workflow Bash")
@pytest.mark.parametrize("event,requested,agent,message,expected", [
    ("push", "true", "", "", "skipped false claude-opus-5-5 0 1 -"),
    ("workflow_dispatch", "false", "", "", "skipped false claude-opus-5-5 1 1 -"),
    ("workflow_dispatch", "true", "", "", "delivered true none 0 0 -"),
    ("workflow_dispatch", "true", "Claude Code", "", "delivered false claude-opus-5-5 0 0 -"),
    # A Claude-Session trailer is sent as a claim for the API to corroborate, with usage cleared.
    ("push", "false", "", SESSION_TRAILER, "delivered false none 0 0 session_01HSAXkLPtdu65CqEWgJgAUF"),
    # An explicit Openshard-Agent wins; the trailer is not consulted.
    ("push", "false", "Claude Code", SESSION_TRAILER, "delivered false claude-opus-5-5 0 0 -"),
    # Only the exact claude.ai session URL trailer counts.
    ("push", "false", "", "Claude-Session: https://example.com/code/session_01HSAXkLPtdu65CqEWgJgAUF\n",
     "skipped false claude-opus-5-5 0 1 -"),
    ("push", "false", "", "Mentions Claude-Session: https://claude.ai/code/session_01HSAXkLPtdu65CqEWgJgAUF\n",
     "skipped false claude-opus-5-5 0 1 -"),
])
def test_unattributed_receipt_needs_explicit_dispatch_and_clears_usage(event, requested, agent, message, expected):
    import shlex

    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    block = re.search(r"^  unattributed=false.*?^    continue\n  fi$", script, re.S | re.M)
    assert block is not None
    assert 'agent: (if $agent == "" then null else $agent end),' in script
    command = f'''set -euo pipefail
GITHUB_EVENT_NAME={event}
OPENSHARD_CAPTURE_UNATTRIBUTED={requested}
GITHUB_STEP_SUMMARY=/dev/null
sha={"a" * 40}
agent={shlex.quote(agent)}
message={shlex.quote(message)}
model=claude-opus-5-5 provider=Anthropic surface=x cost=1 tokens_in=1 tokens_out=1 tokens_cache_read=1 tokens_cache_creation=1
overall_failed=0 skipped=0 outcome=skipped
for _ in 1; do
{block.group(0)}
  outcome=delivered
done
printf '%s %s %s %s %s %s' "$outcome" "$unattributed" "${{model:-none}}" "$overall_failed" "$skipped" "${{claude_session:--}}"
'''
    assert BASH is not None, "CI requires Git Bash; workflow shell checks must execute"
    result = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True, check=True)
    assert result.stdout.splitlines()[-1] == expected


@pytest.mark.skipif(not SHELL_REQUIRED and (BASH is None or not shutil.which("jq")), reason="requires workflow Bash and jq")
@pytest.mark.parametrize("results,budget,expected", [
    ("3 0", 3, "0 2 2"),  # the session's Receipt arrived during the wait
    ("3 3 3 3 3", 3, "3 0 4"),  # still not corroborated: bounded, then reported
    ("3", 0, "3 0 1"),  # the run's wait budget was already spent on an earlier commit
    ("1", 3, "1 3 1"),  # other failures are never retried here
])
def test_uncorroborated_session_waits_a_bounded_time_once_per_run(results, budget, expected, tmp_path):
    workflow = yaml.safe_load((Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text())
    script = next(s["run"] for s in workflow["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")
    loop = re.search(r"^  delivery_rc=0\n  while true; do\n.*?^  done$", script, re.S | re.M)
    assert loop is not None
    command = f'''set -euo pipefail
session_wait_attempts={budget}
OPENSHARD_SESSION_WAIT_SECONDS=0
ACTIONS_ID_TOKEN_REQUEST_TOKEN=t ACTIONS_ID_TOKEN_REQUEST_URL=https://example.invalid/?x=1 OPENSHARD_OIDC_AUDIENCE=a
results=({results})
calls=0
curl() {{ printf '{{"value":"oidc"}}'; }}
deliver_receipt() {{ local rc=${{results[$calls]}}; calls=$((calls + 1)); return "$rc"; }}
{loop.group(0)}
printf '%s %s %s' "$delivery_rc" "$session_wait_attempts" "$calls"
'''
    assert BASH is not None, "CI requires Git Bash; workflow shell checks must execute"
    result = subprocess.run([str(BASH), "-c", command], text=True, capture_output=True, check=True)
    assert result.stdout.splitlines()[-1] == expected
