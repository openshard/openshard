"""Steering in every environment (docs/steering-capture.md, "Every environment").

* Cloud and remote runtimes: the same hooks and session log inside the VM, the
  facts streamed through the remote capture journal, ``environment`` on the block.
* Pull requests: steering derived from reviews and comments (``source: github``),
  with fixture GitHub API responses and the cloud-receipts workflow's delivery.
* Privacy on both paths: no prompt or comment text in the journal, the Receipt,
  the sync payload, the GitHub payload or telemetry.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

from openshard.adapters import steering
from openshard.adapters.agent_env import agent_environment, agent_host, claude_host
from openshard.adapters.claude_hooks import handle_claude_hook, handle_hook
from openshard.github import steering as gh
from openshard.history import steering as steering_view
from openshard.history.receipt_render import render_compact_shard_receipt
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.store import load_history
from openshard.remote import config as rconfig
from openshard.remote import spool
from openshard.sync import envelope
from tests.capture_fixtures import _git, _make_repo

SID = "7c7d7e7f-1a2b-4c3d-8e4f-5a6b7c8d9e0f"
CAPTURE_ID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
ORG = "0f1e2d3c-4b5a-4697-8877-665544332211"
URL = f"https://platform.example.test/v1/remote-captures/{CAPTURE_ID}"
TOKEN = "osr_AbCd1234_" + "t" * 43
SECRET = "sk-ant-api03-" + "Qx7" * 20  # secret-shaped: must never leave the prompt or comment
FOLLOW_UP_PHRASE = "tangerine-nebula"  # only ever in a follow-up prompt
INTERJECTION_PHRASE = "obsidian-kumquat"  # only ever in a message sent mid-turn
COMMENT_PHRASE = "velvet-crankshaft"  # only ever in pull request comments
REVIEWER = "alice-reviewer-login"  # a login: never kept either
TASK_EXCERPT_KEYS = ("task", "task_title", "task_full", "task_short")
SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "platform" / "receipt-sync-envelope.v1.json").read_text(encoding="utf-8")
)
STEERING_SCHEMA = SCHEMA["properties"]["receipt"]["properties"]["steering"]
CLOUD_ENV = {"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_ENTRYPOINT": "remote_web",
             "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_01AbCdEfGhIjKlMn"}


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = _make_repo(tmp_path / "widget")
    _git(root, "remote", "add", "origin", "https://github.com/openshard/widget.git")
    (root / "README.md").write_text("# widget\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "readme")
    return root


@pytest.fixture(autouse=True)
def _claude_config_dir(tmp_path: Path, monkeypatch) -> Path:
    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture
def vm_home(tmp_path: Path, monkeypatch) -> Path:
    """An ephemeral runtime's OpenShard home, attached to a remote capture (no network, no flusher)."""
    home = tmp_path / "vm-home"
    monkeypatch.setenv("OPENSHARD_HOME", str(home))
    monkeypatch.setenv("OPENSHARD_REMOTE_NO_SPAWN", "1")
    for name in (rconfig.URL_ENV, rconfig.TOKEN_ENV, rconfig.DISABLE_ENV):
        monkeypatch.delenv(name, raising=False)
    rconfig.save_attachment(capture_url=URL, token=TOKEN, organisation_id=ORG, agent="claude-code")
    return home


def _now_ms(offset: float = 0.0) -> str:
    return (datetime.now(UTC) + timedelta(seconds=offset)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _entries(repo: Path) -> list[dict]:
    return load_history(repo / ".openshard" / "runs.jsonl", coerce=False)


def _block(entry: dict) -> dict:
    evidence = steering_view.steering_evidence(entry)
    assert evidence is not None
    return evidence


def _transcript() -> Path:
    directory = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "-home-user-widget"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{SID}.jsonl"


def _cloud_claude(repo: Path, event: str, **fields):
    payload = {"session_id": SID, "cwd": str(repo), "hook_event_name": event,
               "transcript_path": str(_transcript()), "permission_mode": "default", **fields}
    return handle_claude_hook(payload, env={"CLAUDE_PROJECT_DIR": str(repo), **CLOUD_ENV})


def _log(*records: dict) -> None:
    with _transcript().open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps({"sessionId": SID, **record}) + "\n")


def _user_text(text: str) -> dict:
    return {"type": "user", "timestamp": _now_ms(), "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _rejection() -> dict:
    body = "The user doesn't want to proceed with this tool use. The tool use was rejected."
    return {"type": "user", "timestamp": _now_ms(), "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_9", "is_error": True, "content": body}]}}


def _wait_next_second() -> None:
    import time

    time.sleep(1.1)


def _cloud_session(repo: Path) -> None:
    """A Claude Code on the web session: a mid-turn message and a rejection in turn 1, an Esc that ends it."""
    _transcript().write_text("", encoding="utf-8")
    _cloud_claude(repo, "SessionStart", source="startup")
    _cloud_claude(repo, "UserPromptSubmit", prompt=f"Build the export button (key {SECRET})")
    _cloud_claude(repo, "PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": ""})
    _log({"type": "queue-operation", "operation": "enqueue", "timestamp": _now_ms(),
          "content": f"also add a CSV option {INTERJECTION_PHRASE} {SECRET}"}, _rejection())
    _cloud_claude(repo, "Stop")  # seals the turn: the log is read in this very fold
    _wait_next_second()
    _cloud_claude(repo, "UserPromptSubmit", prompt=f"no, that's wrong, keep the old button {FOLLOW_UP_PHRASE}")
    _log(_user_text("[Request interrupted by user]"))
    _cloud_claude(repo, "SessionEnd", reason="other")  # the VM goes away right after the Esc


def _journal() -> list[dict]:
    events, _ = spool.pending(None, CAPTURE_ID, limit=spool.MAX_SPOOLED_EVENTS)
    return events


def _steering_kinds(events: list[dict]) -> list[str]:
    return [e["metadata"]["steering_kind"] for e in events if (e.get("metadata") or {}).get("steering_kind")]


def _all_text(root: Path) -> str:
    return "\n".join(p.read_bytes().decode("utf-8", errors="replace") for p in root.rglob("*") if p.is_file())


# ---------------------------------------------------------------------------
# Cloud / remote runtimes
# ---------------------------------------------------------------------------


class TestCloudRuntime:
    def test_cloud_session_streams_every_steering_fact_through_the_journal(self, repo, vm_home):
        _cloud_session(repo)
        journal = _journal()
        assert sorted(_steering_kinds(journal)) == sorted(
            ["initial_prompt", "interjection", "permission_decision", "follow_up", "interrupt"])
        by_kind = {e["metadata"]["steering_kind"]: e for e in journal if (e.get("metadata") or {}).get("steering_kind")}
        assert by_kind["interjection"]["metadata"]["intent"] == "new_task"
        assert by_kind["follow_up"]["metadata"]["intent"] == "correction"
        assert by_kind["permission_decision"]["metadata"]["decision"] == "deny"
        # Read from the log in the fold that sealed turn 1 / ended the session, and still journaled.
        assert by_kind["interjection"]["evidence"] == by_kind["interrupt"]["evidence"] == "agent_reported"
        assert len({e["event_id"] for e in journal}) == len(journal)  # each Event spooled once

    def test_cloud_receipts_carry_the_block_with_environment_cloud_agent(self, repo, vm_home):
        _cloud_session(repo)
        first, second = _entries(repo)
        for entry in (first, second):
            block = _block(entry)
            assert (block["environment"], block["environment_source"]) == ("cloud_agent", "agent_env")
        assert sorted(e["kind"] for e in _block(first)["events"]) == [
            "initial_prompt", "interjection", "permission_decision"]
        assert sorted(e["kind"] for e in _block(second)["events"]) == ["follow_up", "interrupt"]
        doc = envelope.build_envelope(second, 1, core_version="9.9.9")
        assert doc["receipt"]["steering"]["environment"] == "cloud_agent"
        assert doc["receipt"]["ran_on"] == {"kind": "cloud_agent", "name": "claude_code", "source": "agent_env"}
        errors = list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(doc))
        assert not errors, [e.message for e in errors[:3]]

    def test_cursor_cloud_agent_interrupt_hook_is_journaled_and_environment_recorded(self, repo, vm_home):
        env = {"CURSOR_CODE_REMOTE": "true"}

        def cursor(event: str, **fields):
            doc = {"conversation_id": SID, "generation_id": "g1", "hook_event_name": event, "model": "m",
                   "workspace_roots": [str(repo)], **fields}
            handle_hook(doc, env=env, agent="cursor", agent_env=agent_environment("cursor", env))

        cursor("beforeSubmitPrompt", prompt="add a dark mode toggle")
        cursor("stop", status="aborted", loop_count=0)
        assert _steering_kinds(_journal()) == ["initial_prompt", "interrupt"]
        (entry,) = _entries(repo)
        assert _block(entry)["environment"] == "cloud_agent"
        assert entry["capture"]["agent_host"] == "cloud_agent:cursor|"

    def test_hermes_person_approval_is_journaled_as_a_permission_decision(self, repo, vm_home):
        (repo / ".openshard").mkdir(exist_ok=True)  # Hermes hooks are user-global: a repository opts in

        def hermes(event: str, **extra):
            doc = {"hook_event_name": event, "tool_name": None, "tool_input": None, "session_id": SID,
                   "cwd": str(repo), "extra": extra}
            handle_hook(doc, env={}, agent="hermes")

        hermes("pre_llm_call", user_message="tidy the imports")
        hermes("pre_approval_request", command="rm -rf build", surface="cli")
        hermes("post_approval_response", command="rm -rf build", surface="cli", choice="once")
        hermes("pre_approval_request", command="rm -rf dist", surface="cli")
        hermes("post_approval_response", command="rm -rf dist", surface="cli", choice="smart_approve")
        kinds = _steering_kinds(_journal())
        assert kinds.count("permission_decision") == 1  # a smart-mode choice is a model's, not a person's

    def test_log_facts_read_by_the_sealing_fold_reach_the_journal(self, repo, vm_home):
        """Regression: Events staged by the fold that seals and drops the buffer reach the journal."""
        _transcript().write_text("", encoding="utf-8")
        _cloud_claude(repo, "UserPromptSubmit", prompt="build it")
        _log({"type": "queue-operation", "operation": "enqueue", "timestamp": _now_ms(), "content": "also docs"})
        _cloud_claude(repo, "Stop")
        assert "interjection" in _steering_kinds(_journal())

    @pytest.mark.parametrize(("env", "host", "environment"), [
        ({"CLAUDE_CODE_REMOTE": "true", "CLAUDE_CODE_ENTRYPOINT": "remote_web"}, "cloud_agent:claude_code|claude_web",
         "cloud_agent"),
        ({"REPLICAS_WORKSPACE_ID": "ws_1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, "cloud_dev:replicas|terminal",
         "cloud_workspace"),
        ({"CODESPACES": "true", "TERM_PROGRAM": "vscode"}, "cloud_dev:codespaces|vscode", "cloud_workspace"),
        ({"GITHUB_ACTIONS": "true", "CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}, "ci:github_actions|sdk", "ci"),
        ({"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}, "local|vscode", "local_ide"),
        ({"TERMINAL_EMULATOR": "JetBrains-JediTerm"}, "local|jetbrains", "local_ide"),
        ({"CLAUDE_CODE_ENTRYPOINT": "cli", "TERM_PROGRAM": "iTerm.app"}, "local|iterm2", "local_terminal"),
    ])
    def test_claude_code_environment_from_where_it_ran(self, env, host, environment):
        assert claude_host(env) == host
        assert steering.environment_facts(host) == {"environment": environment, "environment_source": "agent_env"}

    @pytest.mark.parametrize(("agent", "env", "facts"), [
        ("codex", {"TERM_PROGRAM": "ghostty"}, {"environment": "local_terminal", "environment_source": "agent_env"}),
        ("codex", {"REPLICAS_WORKSPACE_ID": "ws_1"}, {"environment": "cloud_workspace", "environment_source": "agent_env"}),
        ("cursor", {"CURSOR_CODE_REMOTE": "1"}, {"environment": "cloud_agent", "environment_source": "agent_env"}),
        ("codex", {"CURSOR_CODE_REMOTE": "1"}, {"environment_not_recorded_reason": "local_app_not_recorded"}),
        ("opencode", {}, {"environment_not_recorded_reason": "local_app_not_recorded"}),
        ("claude_code", {"CLAUDE_CODE_ENTRYPOINT": "sdk-py"},
         {"environment_not_recorded_reason": "local_app_not_terminal_or_ide"}),
    ])
    def test_other_agents_environment_is_never_guessed_from_the_agent(self, agent, env, facts):
        assert steering.environment_facts(agent_host(agent, env)) == facts

    def test_no_hook_environment_says_why(self):
        assert steering.environment_facts(None) == {"environment_not_recorded_reason": "where_it_ran_not_recorded"}

    def test_codex_receipt_in_a_replicas_workspace(self, repo, tmp_path, monkeypatch):
        env = {"REPLICAS_WORKSPACE_ID": "ws_1"}
        doc = {"session_id": SID, "turn_id": "t1", "cwd": str(repo), "model": "gpt-5.5"}
        for event, extra in (("UserPromptSubmit", {"prompt": "add a retry"}), ("Stop", {})):
            handle_hook({**doc, "hook_event_name": event, **extra}, env=env, agent="codex",
                        agent_env=agent_environment("codex", env))
        (entry,) = _entries(repo)
        assert _block(entry)["environment"] == "cloud_workspace"
        assert envelope.build_envelope(entry, 0, core_version="9.9.9")["receipt"]["ran_on"]["name"] == "replicas"

    def test_blocks_recorded_before_environments_read_back_unchanged(self):
        old = {"version": 1, "intent_method": "rules-v1", "events": [], "events_dropped": 0, "turn_ends": [],
               "not_available": {}}
        assert set(steering_view.stored_block({"steering": old})) == {
            "version", "intent_method", "events", "events_dropped", "turn_ends", "not_available"}


# ---------------------------------------------------------------------------
# Pull requests: steering from GitHub
# ---------------------------------------------------------------------------

T0 = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)


def _at(hours: float) -> str:
    return (T0 + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _user(login: str, kind: str = "User") -> dict:
    return {"login": login, "type": kind}


def _pr_fixture() -> dict[str, list[dict]]:
    """An agent's PR: c1, review round 1, c2 (the follow-up push), re-request + approval, c3, a late comment."""
    return {
        "commits": [
            {"sha": "1" * 40, "commit": {"committer": {"date": _at(0)}}},
            {"sha": "2" * 40, "commit": {"committer": {"date": _at(2)}}},
            {"sha": "3" * 40, "commit": {"committer": {"date": _at(4)}}},
        ],
        "reviews": [
            {"id": 11, "user": _user(REVIEWER), "state": "CHANGES_REQUESTED", "submitted_at": _at(1),
             "body": f"This breaks the importer {COMMENT_PHRASE} {SECRET}"},
            {"id": 12, "user": _user("bob"), "state": "COMMENTED", "submitted_at": _at(1.2), "body": ""},
            {"id": 13, "user": _user("coderabbitai[bot]", "Bot"), "state": "COMMENTED", "submitted_at": _at(1.3),
             "body": f"Automated summary {COMMENT_PHRASE}"},
            {"id": 14, "user": _user("carol"), "state": "PENDING", "submitted_at": None, "body": "draft"},
            {"id": 15, "user": _user("dave"), "state": "DISMISSED", "submitted_at": _at(1.4), "body": "old"},
            {"id": 16, "user": _user(REVIEWER), "state": "APPROVED", "submitted_at": _at(3), "body": "lgtm"},
        ],
        "review_comments": [
            {"id": 21, "pull_request_review_id": 12, "user": _user("bob"), "created_at": _at(1.2),
             "body": f"why is this synchronous? {COMMENT_PHRASE}"},
            {"id": 22, "pull_request_review_id": 13, "user": _user("coderabbitai[bot]", "Bot"),
             "created_at": _at(1.3), "body": f"nit {COMMENT_PHRASE}"},
        ],
        "issue_comments": [
            {"id": 31, "user": _user(REVIEWER), "created_at": _at(1.5),
             "body": f"@codex fix the flaky test too {COMMENT_PHRASE} {SECRET}"},
            {"id": 32, "user": _user(REVIEWER), "created_at": _at(1.6), "body": f"nice work {COMMENT_PHRASE}"},
            {"id": 33, "user": _user("chatgpt-codex-connector[bot]", "Bot"), "created_at": _at(1.7),
             "body": f"On it {COMMENT_PHRASE}"},
            {"id": 34, "user": _user(REVIEWER), "created_at": _at(1.8), "body": "mail me at x@codex.dev"},
            {"id": 35, "user": _user(REVIEWER), "created_at": _at(5), "body": "@claude also add docs"},
        ],
        "timeline": [
            {"event": "review_requested", "created_at": _at(0.5), "requested_reviewer": {"login": REVIEWER}},
            {"event": "review_requested", "created_at": _at(2.5), "requested_reviewer": {"login": REVIEWER}},
            {"event": "committed", "sha": "2" * 40},
        ],
    }


def _activity() -> gh.Activity:
    pr = _pr_fixture()
    return gh.reduce_activity(reviews=pr["reviews"], review_comments=pr["review_comments"],
                              issue_comments=pr["issue_comments"], timeline=pr["timeline"])


def _commit_block(sha: str) -> dict:
    window = gh.commit_window(_pr_fixture()["commits"], sha)
    assert window is not None
    return gh.build_block(_activity(), window)


class TestGitHubSteering:
    def test_the_follow_up_push_carries_the_review_round_it_answered(self):
        block = _commit_block("2" * 40)
        events = block["events"]
        assert [(e["via"], e["intent"]) for e in events] == [
            ("review_changes_requested", "correction"), ("review_commented", "clarification"),
            ("agent_mention", "unknown")]  # rules-v1 has no rule for a bare "fix ..."
        assert all(e["kind"] == "follow_up" and e["source"] == "github" and e["after"] == "unknown" for e in events)
        assert events[0]["intent_rule"] == "github.review_changes_requested"
        assert block["github"] == {"review_comments": 1, "review_rerequests": 0, "bot_comments_ignored": 3,
                                   "commit_after_review": True, "window_to": _at(2), "window_from": _at(0)}
        assert (block["environment"], block["environment_source"]) == ("web", "github")
        assert set(block["not_available"]) == {
            "initial_prompt", "interjection", "interrupt", "permission_decision", "manual_edit"}

    def test_re_request_and_approval_land_on_the_next_commit(self):
        block = _commit_block("3" * 40)
        assert [(e["via"], e["intent"]) for e in block["events"]] == [("review_approved", "approval")]
        assert block["github"]["review_rerequests"] == 1  # requested again from someone who had reviewed
        assert block["github"]["commit_after_review"] is True

    def test_first_commit_and_late_comments(self):
        first = _commit_block("1" * 40)
        assert first["events"] == [] and first["github"]["commit_after_review"] is False
        assert "window_from" not in first["github"]
        assert gh.commit_window(_pr_fixture()["commits"], "9" * 40) is None  # not this PR's commit: no binding
        late = [i for i in _activity().inputs if i.at > T0 + timedelta(hours=4)]
        assert [i.via for i in late] == ["agent_mention"]  # after the last commit: on no Receipt yet

    def test_a_comment_with_no_author_is_neither_a_person_nor_a_bot(self):
        activity = gh.reduce_activity(
            reviews=[{"id": 1, "user": None, "state": "CHANGES_REQUESTED", "submitted_at": _at(1), "body": "no"}],
            review_comments=[], issue_comments=[{"user": None, "created_at": _at(1), "body": "@codex redo it"}])
        assert activity.inputs == [] and activity.bot_comment_times == []

    def test_autonomy_counts_github_follow_ups_and_unknowns(self):
        payload = gh.payload_block(_commit_block("2" * 40))
        assert payload is not None
        facts = payload["autonomy"]
        assert facts["human_inputs"] == 3
        assert facts["intents"] == {"correction": 1, "clarification": 1, "unknown": 1}
        assert facts["interrupts"] is None and facts["human_inputs_by_kind"]["manual_edit"] is None
        assert "turn_ends" not in payload
        errors = list(jsonschema.Draft202012Validator(STEERING_SCHEMA).iter_errors(payload))
        assert not errors, [e.message for e in errors[:3]]

    def test_steering_for_commit_reads_github_with_get_only_calls(self, tmp_path):
        pr = _pr_fixture()
        calls: list[list[str]] = []
        routes = {"commits/" + "2" * 40 + "/pulls": [{"number": 7, "merged_at": None}],
                  "pulls/7/commits": pr["commits"], "pulls/7/reviews": pr["reviews"],
                  "pulls/7/comments": pr["review_comments"], "issues/7/comments": pr["issue_comments"],
                  "issues/7/timeline": pr["timeline"]}

        def runner(cmd, **_kw):
            calls.append(cmd)
            path = cmd[-1].removeprefix("repos/{owner}/{repo}/").split("?")[0]
            items = routes.get(path)
            if items is None:
                return SimpleNamespace(returncode=1, stdout="")
            return SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(i) for i in items))

        block, reason = gh.steering_for_commit(tmp_path, "2" * 40, runner=runner)
        assert reason == "" and block is not None and len(block["events"]) == 3
        for cmd in calls:
            assert cmd[:2] == ["gh", "api"] and not {"-X", "--method", "-f", "-F", "--input"} & set(cmd)
        assert gh.steering_for_commit(tmp_path, "8" * 40, runner=runner) == (
            None, "no pull request contains this commit")
        assert gh.steering_for_commit(tmp_path, "not-a-sha", runner=runner)[0] is None

    def test_module_entrypoint_prints_null_when_nothing_can_be_derived(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gh.shutil, "which", lambda _name: None)
        assert gh.main(["--commit", "a" * 40]) == 0
        assert capsys.readouterr().out.strip() == "null"


def _workflow_script() -> str:
    workflow = yaml.safe_load(
        (Path(__file__).parents[1] / ".github/workflows/openshard-cloud-receipts.yml").read_text(encoding="utf-8"))
    return next(s["run"] for s in workflow["jobs"]["capture"]["steps"] if s.get("name") == "Verify and create hosted Receipts")


def _workflow_bash() -> str | None:
    from tests.test_cloud_receipt_failure_capture import BASH

    return BASH


class TestWorkflowDelivery:
    def test_the_workflow_derives_steering_per_commit_and_sends_it_as_an_optional_key(self):
        script = _workflow_script()
        assert 'python -m openshard.github.steering --commit "$sha" --pr "$pr_number"' in script
        assert '--argjson steering "${steering:-null}"' in script
        assert "(if $steering == null then {} else {steering: $steering} end)" in script

    @pytest.mark.skipif(_workflow_bash() is None or not shutil.which("jq"), reason="requires workflow Bash and jq")
    @pytest.mark.parametrize("platform", ["accepts", "rejects_steering", "rejects_other"])
    def test_a_platform_that_rejects_steering_gets_the_receipt_without_it(self, platform, tmp_path):
        import hashlib

        sha = "a" * 40
        expected = "rcpt_" + hashlib.sha256(f"github-cloud:\0{42}\0{sha}".encode()).hexdigest()[:32]
        created = json.dumps({"receipt_id": expected})
        first = {"accepts": ("201", created),
                 "rejects_steering": ("400", json.dumps({"error": {"code": "invalid_payload",
                                                                   "message": 'Unrecognized key: "steering"'}})),
                 "rejects_other": ("400", json.dumps({"error": {"code": "invalid_payload", "message": "bad"}}))}[platform]
        sent = tmp_path / "sent.jsonl"
        function = re.search(r"^deliver_receipt\(\) \{.*?^\}", _workflow_script(), re.S | re.M)
        assert function is not None
        command = "set -euo pipefail\n" + function.group(0) + f'''
GITHUB_REPOSITORY_ID=42
sha={sha}
OPENSHARD_API_BASE=https://example.invalid
payload='{{"head_sha":"{sha}","steering":{{"version":1}}}}'
oidc_token=not-a-real-token
verification_status=passed
counter={shlex.quote(str(tmp_path / "calls"))}
curl() {{
  local out="" data=""
  while [[ $# -gt 0 ]]; do
    case "$1" in -o) out="$2"; shift 2;; --data) data="$2"; shift 2;; *) shift;; esac
  done
  printf '%s\\n' "$data" >> {shlex.quote(str(sent))}
  echo x >> "$counter"  # curl runs in a command substitution: count in a file
  if [[ $(wc -l < "$counter") -eq 1 ]]; then printf '%s' {shlex.quote(first[1])} > "$out"; printf '%s' {first[0]}
  else printf '%s' {shlex.quote(created)} > "$out"; printf '201'; fi
}}
result=0
deliver_receipt || result=$?
printf '%s' "$result"
'''
        bash = _workflow_bash()
        assert bash is not None
        result = subprocess.run([bash, "-c", command], text=True, capture_output=True, check=False)
        payloads = [json.loads(line) for line in sent.read_text(encoding="utf-8").splitlines()]
        if platform == "accepts":
            assert result.stdout.endswith("0") and len(payloads) == 1 and "steering" in payloads[0]
        elif platform == "rejects_steering":
            assert result.stdout.endswith("0"), result.stderr
            assert ["steering" in p for p in payloads] == [True, False]  # resent once, without it
        else:
            assert result.stdout.endswith("1") and len(payloads) == 1  # any other rejection still fails


# ---------------------------------------------------------------------------
# Privacy: no text on the remote or GitHub paths
# ---------------------------------------------------------------------------


class TestPrivacy:
    def test_cloud_session_text_never_reaches_journal_receipt_sync_or_telemetry(self, repo, vm_home, monkeypatch):
        from openshard.telemetry import queue as telemetry_queue
        from openshard.telemetry import state as telemetry_state

        monkeypatch.delenv("OPENSHARD_TELEMETRY", raising=False)
        telemetry_state.set_consent("on", source="cli")
        telemetry_queue.clear()
        _cloud_session(repo)

        surfaces: dict[str, str] = {
            "journal": json.dumps(_journal()),
            "vm-home": _all_text(vm_home),  # the spool, its state and everything else in the VM's OpenShard home
            "repo/.openshard": _all_text(repo / ".openshard"),
            "telemetry": json.dumps(telemetry_queue.peek(limit=500)),
        }
        assert _steering_kinds(_journal()), "the journal holds steering facts"
        for i, entry in enumerate(_entries(repo)):
            receipt = envelope.build_envelope(entry, i, core_version="9.9.9")["receipt"]
            surfaces[f"sync/{i}"] = json.dumps(receipt)
            surfaces[f"receipt/{i}"] = render_compact_shard_receipt(build_shard_receipt(entry, index=i))
            surfaces[f"steering/{i}"] = json.dumps(entry["capture"]["steering"])
            surfaces[f"sync-without-task/{i}"] = json.dumps(
                {k: v for k, v in receipt.items() if k not in TASK_EXCERPT_KEYS})
        for name, text in surfaces.items():
            assert SECRET not in text and SECRET[:24] not in text, name
            assert INTERJECTION_PHRASE not in text, name  # a mid-turn message is never stored or sent
            if name in ("journal", "telemetry") or name.startswith(("steering/", "sync-without-task/")):
                # Only the pre-existing task excerpt of the prompt that opens a Receipt may hold its words.
                assert FOLLOW_UP_PHRASE not in text, name

    def test_pull_request_text_and_logins_never_reach_the_block_payload_or_output(self, tmp_path, monkeypatch, capsys):
        pr = _pr_fixture()

        def runner(cmd, **_kw):
            path = cmd[-1].removeprefix("repos/{owner}/{repo}/").split("?")[0]
            items = {"pulls/7/commits": pr["commits"], "pulls/7/reviews": pr["reviews"],
                     "pulls/7/comments": pr["review_comments"], "issues/7/comments": pr["issue_comments"],
                     "issues/7/timeline": pr["timeline"]}.get(path, [])
            return SimpleNamespace(returncode=0, stdout="\n".join(json.dumps(i) for i in items))

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(gh.shutil, "which", lambda _name: "/usr/bin/gh")
        monkeypatch.setattr(gh.subprocess, "run", runner)
        assert gh.main(["--commit", "2" * 40, "--pr", "7"]) == 0
        printed = capsys.readouterr().out
        block, _ = gh.steering_for_commit(tmp_path, "2" * 40, pr=7, runner=runner)
        surfaces = {"printed": printed, "block": json.dumps(block), "payload": json.dumps(gh.payload_block(block or {})),
                    "activity": repr(_activity())}
        assert json.loads(printed)["events"], "the printed payload holds the derived events"
        for name, text in surfaces.items():
            assert SECRET not in text and SECRET[:24] not in text, name
            assert COMMENT_PHRASE not in text, name
            assert REVIEWER not in text and "coderabbit" not in text, name


# ---------------------------------------------------------------------------
# Docs: the environment matrix is the code's
# ---------------------------------------------------------------------------


def test_environment_matrix_in_docs_matches_the_code():
    doc = (Path(__file__).parent.parent / "docs" / "steering-capture.md").read_text(encoding="utf-8")
    labels = {"live_hook": "live hook", "from_log": "from log", "from_git": "from git",
              "from_github": "from GitHub", "not_available": "not available"}
    names = {"claude_code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "opencode": "OpenCode",
             "antigravity": "Google Antigravity", "grok_build": "Grok Build", "hermes": "Hermes Agent",
             "grok_bot": "Grok Bot", "pull_request_agent": "Any PR-driven agent"}
    envs = "|".join(steering_view.ENVIRONMENTS)
    rows = [line for line in doc.splitlines() if re.match(rf"^\| [^|`]+ \| `({envs})`( \([A-Z]\))? \|", line)]
    assert len(rows) == len(steering.ENVIRONMENT_ROWS)
    for (agent, environment), row in zip(steering.ENVIRONMENT_ROWS, rows, strict=True):
        cells = [c.strip() for c in row.strip("|").split("|")]
        assert cells[0] == names[agent] and cells[1].startswith(f"`{environment}`"), row
        for cell, signal in zip(cells[2:], steering.SIGNALS, strict=True):
            assert cell.startswith(labels[steering.environment_capability(agent, environment, signal)[0]]), (
                agent, environment, signal, cell)
    for environment in steering_view.ENVIRONMENTS:
        assert f"| `{environment}` |" in doc, environment
