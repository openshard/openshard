"""The merge gate: decision table, Platform client, agent marks and ``openshard ci gate``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from openshard.ci import gate_platform as gp
from openshard.ci.agent_marks import agent_marks
from openshard.ci.merge_gate import HUMAN_CHANGE, decide, render_receipt
from openshard.cli.main import cli
from openshard.verification import ci_evidence as ci
from openshard.verification import post_session as ps

SHA = "a" * 40
ORG = "0190d3a2-1c3e-7000-8b1a-0242ac120002"
OIDC_URL = "https://token.actions.example/token?api-version=2.0"


def _verdict(status: str, *, sha: str = SHA, policy: str = "agent_prs", receipts: list | None = None,
             merge_group: dict | None = None, reason: str | None = None) -> dict:
    return {"schema": gp.SCHEMA, "sha": sha, "repository": "o/r", "status": status,
            "policy": {"require_receipt": policy}, "receipts": receipts or [], "merge_group": merge_group,
            "reason": reason, "checked_at": "2026-10-21T10:00:00Z"}


FULL_RECEIPT = {
    "receipt_id": "rcpt_1", "url": "https://app.openshard.dev/o/receipts/rcpt_1", "status": "verified",
    "agent": "Claude Code", "model": "claude-opus-5-5", "task": "Fix the widget",
    "files_changed": 3, "lines_added": 40, "lines_removed": 2,
    "checks": [{"name": "pytest", "outcome": "passed", "source": "independently_verified"},
               {"name": "ruff", "outcome": "failed", "source": "agent_reported"}],
    "verification": {"result": "verified", "verified_at": "2026-10-21T09:59:00Z", "bound_commit": SHA},
    "cost": {"usd": 0.4213, "source": "official_rate_estimate"},
    "approvals": [{"requires_approval": True, "request_source": "policy", "granted": True,
                   "decision_source": "human", "decision_reason": None}],
    "witnessed": True, "signature_verified": True,
    "merge_check": {"base_ref": "origin/main", "base_sha": "b" * 40, "head_sha": SHA,
                    "checked_at": "2026-10-21T09:00:00Z", "behind_by": 2, "base_commits": [], "conflicts": []},
}
EMPTY_RECEIPT = {"receipt_id": "rcpt_2", "url": None, "status": "pending", "agent": None, "model": None, "task": None,
                 "files_changed": None, "lines_added": None, "lines_removed": None, "checks": [],
                 "verification": {"result": "not_recorded", "verified_at": None, "bound_commit": None},
                 "cost": {"usd": None, "source": None}, "approvals": None,
                 "witnessed": False, "signature_verified": None, "merge_check": None}
FIXTURES = Path(__file__).parent / "fixtures" / "platform"
EXAMPLE = json.loads((FIXTURES / "gate-verdict.example.json").read_text(encoding="utf-8"))
QUEUE_SHA = "4" * 40


# --- decision table ---------------------------------------------------------

class TestDecision:
    def test_verified_passes(self):
        r = decide(SHA, _verdict("verified"))
        assert (r.status, r.exit_code, r.verdict) == ("pass", 0, "verified")

    def test_failed_fails(self):
        r = decide(SHA, _verdict("failed"))
        assert (r.status, r.exit_code) == ("fail", 1) and "verification failed" in r.headline

    def test_pending_at_the_end_fails(self):
        assert decide(SHA, _verdict("pending")).status == "fail"

    def test_none_with_agent_marks_fails(self):
        r = decide(SHA, _verdict("none"), agent_marks=["Openshard-Agent"])
        assert r.status == "fail" and "an agent changed code but there is no Receipt" in r.headline
        assert "Openshard-Agent" in r.headline and "waited" not in r.headline

    def test_none_with_agent_marks_says_how_long_it_waited(self):
        r = decide(SHA, _verdict("none"), agent_marks=["Openshard-Agent"], waited_minutes=10)
        assert r.status == "fail" and r.headline.endswith("The gate waited 10 min for one.")

    def test_none_without_marks_under_agent_prs_passes_as_human(self):
        r = decide(SHA, _verdict("none", policy="agent_prs"), agent_marks=[])
        assert (r.status, r.headline) == ("pass", HUMAN_CHANGE)

    def test_none_under_all_prs_fails(self):
        r = decide(SHA, _verdict("none", policy="all_prs"), agent_marks=[])
        assert r.status == "fail" and "every pull request" in r.headline

    def test_none_with_unreadable_commits_fails(self):
        r = decide(SHA, _verdict("none"), agent_marks=None)
        assert r.status == "fail" and "fetch-depth: 0" in r.headline

    @pytest.mark.parametrize("local,status", [("verified", "pass"), ("failed", "fail")])
    def test_trusted_local_evidence_decides_when_the_platform_has_none(self, local, status):
        assert decide(SHA, _verdict("none", policy="all_prs"), local_verdict=local, agent_marks=[]).status == status

    def test_platform_evidence_wins_over_local(self):
        assert decide(SHA, _verdict("verified"), local_verdict="failed").status == "pass"
        assert decide(SHA, _verdict("failed"), local_verdict="verified").status == "fail"

    @pytest.mark.parametrize("kind", [gp.ERROR_UNREACHABLE, gp.ERROR_AUTH, gp.ERROR_FORK, gp.ERROR_NO_CREDENTIALS,
                                      gp.ERROR_BAD_RESPONSE, gp.ERROR_TIMEOUT, gp.ERROR_REJECTED, "error"])
    def test_every_error_fails_with_its_reason(self, kind):
        r = decide(SHA, gp.PlatformError(kind, "Plain reason here."), local_verdict="verified", agent_marks=[])
        assert (r.status, r.exit_code, r.verdict) == ("fail", 1, kind)
        assert "Plain reason here." in r.headline and "Plain reason here." in r.summary_markdown


# --- summary ----------------------------------------------------------------

class TestSummary:
    def test_every_field_is_shown(self):
        md = "\n".join(render_receipt(FULL_RECEIPT))
        assert "[open in Openshard](https://app.openshard.dev/o/receipts/rcpt_1)" in md
        for row in ("| Agent | Claude Code |", "| Model | claude-opus-5-5 |", "| Task | Fix the widget |",
                    "| Files changed | 3 |", "| Lines | +40 / -2 |",
                    "| Verification | verified, at 2026-10-21T09:59:00Z, bound to commit aaaaaaaaaaaa |",
                    "| Cost | \\$0.42 (official\\_rate\\_estimate) |",
                    "| Approvals | requires\\_approval: True, request\\_source: policy, granted: True, "
                    "decision\\_source: human |",
                    "| Platform-witnessed | yes |", "| Signature verified | yes |",
                    "| pytest | passed | independently\\_verified |", "| ruff | failed | agent\\_reported |",
                    "| Merge check (informational) | no conflicts with origin/main, 2 commit(s) behind"):
            assert row in md, row
        assert "detectable" in md and "not being verified" in md

    def test_unknown_is_unknown_never_zero(self):
        md = "\n".join(render_receipt(EMPTY_RECEIPT))
        for row in ("Agent", "Model", "Task", "Files changed", "Lines", "Cost", "Approvals", "Signature verified"):
            assert f"| {row} | unknown |" in md, row
        assert "| Verification | not\\_recorded, time unknown, bound commit unknown |" in md
        assert "| Platform-witnessed | no |" in md and "Checks: none recorded." in md
        lenient = "\n".join(render_receipt({**EMPTY_RECEIPT, "checks": None, "verification": None}))
        assert "| Verification | unknown |" in lenient and "Checks: unknown." in lenient
        assert "$0" not in md and "| 0 |" not in md and "detectable" not in md
        assert "open in Openshard" not in md  # no URL, no link

    def test_partial_counts_stay_partial(self):
        md = "\n".join(render_receipt({**EMPTY_RECEIPT, "lines_added": 5}))
        assert "| Lines | +5 / -unknown |" in md

    def test_empty_lists_are_none_recorded(self):
        md = "\n".join(render_receipt({**EMPTY_RECEIPT, "approvals": [], "checks": []}))
        assert "| Approvals | none recorded |" in md and "Checks: none recorded." in md

    def test_a_real_zero_cost_is_shown(self):
        assert "| Cost | \\$0.0000 (provider) |" in "\n".join(render_receipt(
            {**EMPTY_RECEIPT, "cost": {"usd": 0, "source": "provider"}}))

    def test_hostile_text_cannot_inject(self):
        hostile = "[x](https://evil.test) <img src=x> **b** | \x1b[31m # h"
        md = "\n".join(render_receipt({**EMPTY_RECEIPT, "agent": hostile, "task": "x" * 500,
                                       "url": "javascript:alert(1)"}))
        row = next(line for line in md.splitlines() if line.startswith("| Agent |"))
        assert "<" not in row and "\x1b" not in row and " " not in row
        assert "\\[x\\]" in row and "\\*\\*b\\*\\*" in row and "\\|" in row
        assert "javascript" not in md
        task = next(line for line in md.splitlines() if line.startswith("| Task |"))
        assert len(task) < 200 and task.endswith("… |")

    def test_decide_lists_every_receipt(self):
        md = decide(SHA, _verdict("failed", receipts=[FULL_RECEIPT, EMPTY_RECEIPT])).summary_markdown
        assert "#### Receipt rcpt\\_1" in md and "#### Receipt rcpt\\_2" in md
        assert "Platform answer: failed" in md


class TestPlatformContract:
    """Core's parser against the Platform's published schema (platform packages/contracts, copied here)."""

    def test_fixtures_match_the_published_schema(self):
        import jsonschema

        schema = json.loads((FIXTURES / "gate-verdict.v1.json").read_text(encoding="utf-8"))
        for doc in (EXAMPLE, _verdict("verified", receipts=[FULL_RECEIPT, EMPTY_RECEIPT]),
                    _verdict("none", reason="Merge queue ref names no pull request head.")):
            jsonschema.validate(doc, schema)

    def test_the_real_example_parses_and_passes(self):
        out = gp.validate(json.loads(json.dumps(EXAMPLE)), QUEUE_SHA)
        assert isinstance(out, dict)
        r = decide(QUEUE_SHA, out)
        assert r.status == "pass"
        md = r.summary_markdown
        assert "Merge queue: PR \\#7 head `aaaaaaaaaaaa` (queue commit `444444444444`)." in md
        assert "[open in Openshard](https://app.openshard.dev/receipts/rcpt_29c27d6dc7309152fe77c9ff6e0a979d)" in md
        for row in ("| Agent | Codex |", "| Model | unknown |", "| Cost | unknown |", "| Approvals | unknown |",
                    "| Signature verified | unknown |", "| Platform-witnessed | no |",
                    "| Verification | passed, at 2026-10-20T12:00:05Z, bound to commit aaaaaaaaaaaa |",
                    "| tests | passed | independently\\_verified |"):
            assert row in md, row
        assert "detectable" not in md

    def test_a_merge_queue_answer_must_name_the_requested_commit(self):
        assert isinstance(gp.validate(EXAMPLE, SHA), gp.PlatformError)  # asked for the head, answer names the queue
        wrong_head = {**EXAMPLE, "merge_group": {**EXAMPLE["merge_group"], "head_sha": "b" * 40}}
        assert isinstance(gp.validate(wrong_head, QUEUE_SHA), gp.PlatformError)
        assert isinstance(gp.validate({**EXAMPLE, "merge_group": {"requested_sha": QUEUE_SHA}}, QUEUE_SHA),
                          gp.PlatformError)
        assert isinstance(gp.validate({**EXAMPLE, "merge_group": None}, QUEUE_SHA), gp.PlatformError)

    def test_reason_is_shown_when_none(self):
        md = decide(SHA, _verdict("none", reason="No pull request head in the queue ref."), agent_marks=[]).summary_markdown
        assert "Platform reason: No pull request head in the queue ref." in md

    def test_a_partial_pass_times_out_as_incomplete_and_shows_the_receipt(self):
        partial = {**EMPTY_RECEIPT, "verification": {"result": "verified_partial", "verified_at": None,
                                                      "bound_commit": SHA}}
        out = gp.poll(lambda: _verdict("pending", receipts=[partial]), wait_seconds=0, interval=1,
                      sleep=lambda _s: None, monotonic=lambda: 0.0)
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_TIMEOUT
        assert out.message.startswith("Verification incomplete (partial) after 0 min") and "verified_partial" in out.message
        r = decide(SHA, out)
        assert r.status == "fail" and "#### Receipt rcpt\\_2" in r.summary_markdown
        assert "Platform answer: pending" in r.summary_markdown


# --- agent marks --------------------------------------------------------------

class TestAgentMarks:
    def test_trailers_are_found(self):
        text = ("Fix it\n\nOpenshard-Agent: Claude Code\nopenshard-tokens-input: 12\n"
                "Claude-Session: https://claude.ai/code/session_01KeyaUufFTUHJ9CwcMbYXBV\n")
        assert agent_marks(text) == ["Openshard-Agent", "Openshard-Tokens-Input", "Claude-Session"]

    @pytest.mark.parametrize("text", ["", "Fix the agent docs", "Openshard-Agent:", "Claude-Session: nope",
                                      "mentions Openshard-Agent: inline", None])
    def test_non_marks_are_ignored(self, text):
        assert agent_marks(text) == []


# --- Platform client ----------------------------------------------------------

class TestCredentials:
    def test_api_key_needs_an_org(self):
        assert isinstance(gp.credentials_from_env({"OPENSHARD_GATE_API_KEY": "osk_x"}), gp.PlatformError)
        creds = gp.credentials_from_env({"OPENSHARD_GATE_API_KEY": "osk_x", "OPENSHARD_GATE_ORG": ORG})
        assert creds == gp.Credentials(api_key="osk_x", org=ORG)

    def test_oidc(self):
        creds = gp.credentials_from_env({"ACTIONS_ID_TOKEN_REQUEST_URL": OIDC_URL,
                                         "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"})
        assert creds == gp.Credentials(oidc_url=OIDC_URL, oidc_request_token="req")

    def test_fork_and_missing(self):
        fork = gp.credentials_from_env({"OPENSHARD_GATE_FORK": "true"})
        assert isinstance(fork, gp.PlatformError) and fork.message == gp.FORK_MESSAGE
        none = gp.credentials_from_env({})
        assert isinstance(none, gp.PlatformError) and "id-token: write" in none.message


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _platform(responses: list, seen: list | None = None):
    """A handler: OIDC mint always works; each Platform call takes the next response."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "token.actions.example":
            if seen is not None:
                seen.append(("oidc", str(request.url), request.headers.get("authorization")))
            return httpx.Response(200, json={"value": "jwt-for-gate"})
        if seen is not None:
            seen.append(("api", str(request.url), request.headers.get("authorization")))
        item = responses.pop(0) if len(responses) > 1 else responses[0]
        if isinstance(item, Exception):
            raise item
        status, body = item
        return httpx.Response(status, json=body)
    return handler


OIDC = gp.Credentials(oidc_url=OIDC_URL, oidc_request_token="req")


class TestFetch:
    def test_oidc_request_and_audience(self):
        seen: list = []
        out = gp.fetch_once("https://api.example/", SHA, OIDC, _client(_platform([(200, _verdict("verified"))], seen)))
        assert isinstance(out, dict) and out["status"] == "verified"
        assert seen[0] == ("oidc", OIDC_URL + "&audience=openshard-gate", "Bearer req")
        assert seen[1] == ("api", f"https://api.example/v1/cloud/github/gate-verdict?sha={SHA}", "Bearer jwt-for-gate")

    def test_api_key_route(self):
        seen: list = []
        creds = gp.Credentials(api_key="osk_x", org=ORG)
        gp.fetch_once("https://api.example", SHA, creds, _client(_platform([(200, _verdict("none"))], seen)))
        assert seen == [("api", f"https://api.example/v1/orgs/{ORG}/commits/{SHA}/gate-verdict", "Bearer osk_x")]

    def test_oidc_mint_failure_is_auth(self):
        def handler(request):
            return httpx.Response(403, json={})
        out = gp.fetch_once("https://api.example", SHA, OIDC, _client(handler))
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_AUTH and not out.transient

    @pytest.mark.parametrize("status,kind,transient", [
        (401, gp.ERROR_AUTH, False), (403, gp.ERROR_AUTH, False), (404, gp.ERROR_REJECTED, False),
        (429, gp.ERROR_UNREACHABLE, True), (503, gp.ERROR_UNREACHABLE, True)])
    def test_http_errors(self, status, kind, transient):
        body = {"error": {"code": "x", "message": "Repository is not connected\nto an organisation."}}
        out = gp.fetch_once("https://api.example", SHA, OIDC, _client(_platform([(status, body)])))
        assert isinstance(out, gp.PlatformError) and (out.kind, out.transient) == (kind, transient)
        assert f"HTTP {status}" in out.message and "Repository is not connected to an organisation." in out.message

    def test_transport_error_is_unreachable(self):
        out = gp.fetch_once("https://api.example", SHA, OIDC, _client(_platform([httpx.ConnectError("down")])))
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_UNREACHABLE and out.transient

    @pytest.mark.parametrize("body", [
        {}, {**_verdict("verified"), "schema": "other"}, _verdict("verified", sha="b" * 40),
        {**_verdict("verified"), "status": "maybe"}, {**_verdict("none"), "policy": {"require_receipt": "x"}},
        {**_verdict("none"), "receipts": "nope"}])
    def test_malformed_answers_are_errors(self, body):
        out = gp.fetch_once("https://api.example", SHA, OIDC, _client(_platform([(200, body)])))
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_BAD_RESPONSE


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


class TestPoll:
    def _poll(self, results: list, wait: float = 60, interval: float = 15):
        clock = _Clock()
        calls = iter(results)
        out = gp.poll(lambda: next(calls), wait_seconds=wait, interval=interval, sleep=clock.sleep,
                      monotonic=lambda: clock.now)
        return out, clock

    def test_pending_then_final(self):
        out, clock = self._poll([_verdict("pending"), _verdict("pending"), _verdict("failed")])
        assert out["status"] == "failed" and clock.sleeps == [15, 15]

    def test_pending_until_timeout(self):
        out, clock = self._poll([_verdict("pending")] * 10, wait=40)
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_TIMEOUT and out.last is not None
        assert out.message == "Verification did not finish in 0.666667 min." and sum(clock.sleeps) == 40

    def test_zero_wait_times_out_at_once(self):
        out, clock = self._poll([_verdict("pending")], wait=0)
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_TIMEOUT and clock.sleeps == []

    def test_transient_errors_are_retried(self):
        blip = gp.PlatformError(gp.ERROR_UNREACHABLE, "down", transient=True)
        out, _ = self._poll([blip, _verdict("verified")])
        assert out["status"] == "verified"
        out, _ = self._poll([blip] * 10, wait=30)
        assert isinstance(out, gp.PlatformError) and out.kind == gp.ERROR_UNREACHABLE and "Still failing" in out.message

    def test_none_is_final_by_default(self):
        out, clock = self._poll([_verdict("none"), _verdict("verified")])
        assert out["status"] == "none" and clock.sleeps == []

    def test_wait_on_none_until_a_receipt_appears(self):
        clock = _Clock()
        calls = iter([_verdict("none"), _verdict("none"), _verdict("pending"), _verdict("verified")])
        out = gp.poll(lambda: next(calls), wait_seconds=600, interval=15, sleep=clock.sleep,
                      monotonic=lambda: clock.now, wait_on_none=True)
        assert out["status"] == "verified" and clock.sleeps == [15, 15, 15]

    def test_wait_on_none_returns_none_at_the_deadline(self):
        clock = _Clock()
        out = gp.poll(lambda: _verdict("none"), wait_seconds=40, interval=15, sleep=clock.sleep,
                      monotonic=lambda: clock.now, wait_on_none=True)
        assert isinstance(out, dict) and out["status"] == "none" and sum(clock.sleeps) == 40

    def test_permanent_errors_stop_at_once(self):
        auth = gp.PlatformError(gp.ERROR_AUTH, "no")
        out, clock = self._poll([auth])
        assert out is auth and clock.sleeps == []


# --- the CLI ------------------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # CI runners may carry real OIDC and event variables; the gate must never see them here.
    for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "OPENSHARD_GATE_API",
                 "OPENSHARD_GATE_API_KEY", "OPENSHARD_GATE_ORG", "OPENSHARD_GATE_FORK", "GITHUB_EVENT_PATH",
                 "GITHUB_STEP_SUMMARY", "GITHUB_OUTPUT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "factory"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".openshard/\n", encoding="utf-8")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return root


def _commit(repo: Path, message: str) -> str:
    (repo / "a.py").write_text((repo / "a.py").read_text(encoding="utf-8") + "y = 2\n", encoding="utf-8")
    _git(repo, "commit", "-qam", message)
    return _git(repo, "rev-parse", "HEAD")


def _serve(monkeypatch, responses: list, seen: list | None = None) -> None:
    handler = _platform(responses, seen)
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler)))


def _gate(repo: Path, monkeypatch, *args: str, oidc: bool = True, event: dict | None = None,
          tmp: Path | None = None, extra_env: dict | None = None):
    monkeypatch.chdir(repo)
    env = dict(extra_env or {})
    if oidc:
        env.update(ACTIONS_ID_TOKEN_REQUEST_URL=OIDC_URL, ACTIONS_ID_TOKEN_REQUEST_TOKEN="req")
    if event is not None and tmp is not None:
        path = tmp / "event.json"
        path.write_text(json.dumps(event), encoding="utf-8")
        env["GITHUB_EVENT_PATH"] = str(path)
    return CliRunner().invoke(cli, ["ci", "gate", "--poll-seconds", "0.1", *args], env=env)


class TestCli:
    def test_verified_passes_and_writes_the_summary(self, repo, monkeypatch, tmp_path):
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("verified", sha=head, receipts=[{**FULL_RECEIPT}]))])
        summary, output = tmp_path / "s.md", tmp_path / "o.txt"
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--github-step-summary", "--github-output",
                    extra_env={"GITHUB_STEP_SUMMARY": str(summary), "GITHUB_OUTPUT": str(output)})
        assert res.exit_code == 0 and res.output.startswith("Openshard gate: pass")
        assert "#### Receipt rcpt\\_1" in summary.read_text(encoding="utf-8")
        assert output.read_text(encoding="utf-8") == "openshard_gate_status=pass\nopenshard_gate_verdict=verified\n"

    def test_failed_fails(self, repo, monkeypatch):
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("failed", sha=head))])
        assert _gate(repo, monkeypatch, "--commit", head).exit_code == 1

    def test_human_change_passes(self, repo, monkeypatch, tmp_path):
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "a human change")
        _serve(monkeypatch, [(200, _verdict("none", sha=head))])
        event = {"pull_request": {"base": {"sha": base, "repo": {"full_name": "o/r"}},
                                  "head": {"sha": head, "repo": {"full_name": "o/r"}}, "body": "No marks."}}
        res = _gate(repo, monkeypatch, "--commit", head, event=event, tmp=tmp_path)
        assert res.exit_code == 0 and HUMAN_CHANGE in res.output

    def test_agent_trailer_without_receipt_fails(self, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "agent work\n\nOpenshard-Agent: Codex")
        head = _commit(repo, "plain follow-up")  # the mark is on an earlier commit in the range
        _serve(monkeypatch, [(200, _verdict("none", sha=head))])
        res = _gate(repo, monkeypatch, "--commit", head, "--base", base, "--json", "--wait-minutes", "0")
        body = json.loads(res.output)
        assert res.exit_code == 1 and body["agent_marks"] == ["Openshard-Agent"]

    def test_agent_change_waits_for_its_receipt(self, repo, monkeypatch):
        # The race seen on #630: the gate asks before Cloud Receipts has delivered.
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "agent work\n\nOpenshard-Agent: Claude Code")
        seen: list = []
        _serve(monkeypatch, [(200, _verdict("none", sha=head)), (200, _verdict("none", sha=head)),
                             (200, _verdict("verified", sha=head, receipts=[{**FULL_RECEIPT}]))], seen)
        res = _gate(repo, monkeypatch, "--commit", head, "--base", base, "--wait-minutes", "1")
        assert res.exit_code == 0 and res.output.startswith("Openshard gate: pass")
        assert [s[0] for s in seen].count("api") == 3

    def test_agent_change_still_without_receipt_fails_after_the_wait(self, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "agent work\n\nOpenshard-Agent: Claude Code")
        seen: list = []
        _serve(monkeypatch, [(200, _verdict("none", sha=head))], seen)
        res = _gate(repo, monkeypatch, "--commit", head, "--base", base, "--wait-minutes", "0.01")
        assert res.exit_code == 1 and "an agent changed code but there is no Receipt" in res.output
        assert "The gate waited 0.01 min for one." in res.output
        assert [s[0] for s in seen].count("api") > 1

    @pytest.mark.parametrize("policy,code", [("agent_prs", 0), ("all_prs", 1)])
    def test_human_change_is_decided_at_once(self, repo, monkeypatch, policy, code):
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "a human change")
        seen: list = []
        _serve(monkeypatch, [(200, _verdict("none", sha=head, policy=policy)),
                             (200, _verdict("verified", sha=head))], seen)
        res = _gate(repo, monkeypatch, "--commit", head, "--base", base, "--wait-minutes", "10")
        assert res.exit_code == code and [s[0] for s in seen].count("api") == 1

    def test_agent_mark_in_the_pr_body_counts(self, repo, monkeypatch, tmp_path):
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("none", sha=head))])
        event = {"pull_request": {"base": {"sha": base}, "head": {"sha": head}, "body": "Openshard-Agent: Claude Code"}}
        assert _gate(repo, monkeypatch, "--commit", head, "--wait-minutes", "0", event=event, tmp=tmp_path).exit_code == 1

    def test_unreadable_commit_range_fails(self, repo, monkeypatch):
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("none", sha=head))])
        res = _gate(repo, monkeypatch, "--commit", head, "--base", "c" * 40)
        assert res.exit_code == 1 and "fetch-depth: 0" in res.output

    def test_all_prs_policy_fails_a_human_change(self, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("none", sha=head, policy="all_prs"))])
        assert _gate(repo, monkeypatch, "--commit", head, "--base", base).exit_code == 1

    def test_pending_then_verified(self, repo, monkeypatch):
        head = _commit(repo, "change")
        seen: list = []
        _serve(monkeypatch, [(200, _verdict("pending", sha=head)), (200, _verdict("verified", sha=head))], seen)
        res = _gate(repo, monkeypatch, "--commit", head, "--wait-minutes", "1")
        assert res.exit_code == 0 and [s[0] for s in seen] == ["oidc", "api", "oidc", "api"]  # fresh token per try

    def test_pending_times_out(self, repo, monkeypatch):
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("pending", sha=head))])
        res = _gate(repo, monkeypatch, "--commit", head, "--wait-minutes", "0")
        assert res.exit_code == 1 and "Verification did not finish in 0 min." in res.output

    def test_unreachable_fails(self, repo, monkeypatch):
        head = _commit(repo, "change")
        _serve(monkeypatch, [httpx.ConnectError("down")])
        res = _gate(repo, monkeypatch, "--commit", head, "--wait-minutes", "0")
        assert res.exit_code == 1 and "unreachable" in res.output

    def test_auth_failure_fails(self, repo, monkeypatch):
        head = _commit(repo, "change")
        _serve(monkeypatch, [(401, {"error": {"message": "Unknown audience."}})])
        res = _gate(repo, monkeypatch, "--commit", head)
        assert res.exit_code == 1 and "refused" in res.output and "Unknown audience." in res.output

    def test_fork_without_credentials_fails_with_the_fork_reason(self, repo, monkeypatch, tmp_path):
        head = _commit(repo, "change")
        event = {"pull_request": {"base": {"sha": head, "repo": {"full_name": "o/r"}},
                                  "head": {"sha": head, "repo": {"full_name": "someone/r"}}}}
        res = _gate(repo, monkeypatch, "--commit", head, oidc=False, event=event, tmp=tmp_path)
        assert res.exit_code == 1 and gp.FORK_MESSAGE in res.output

    def test_no_credentials_fails(self, repo, monkeypatch):
        res = _gate(repo, monkeypatch, "--commit", "HEAD", oidc=False)
        assert res.exit_code == 1 and "id-token: write" in res.output

    def test_api_key_variant(self, repo, monkeypatch):
        head = _commit(repo, "change")
        seen: list = []
        _serve(monkeypatch, [(200, _verdict("verified", sha=head))], seen)
        res = _gate(repo, monkeypatch, "--commit", head, oidc=False,
                    extra_env={"OPENSHARD_GATE_API_KEY": "osk_x", "OPENSHARD_GATE_ORG": ORG,
                               "OPENSHARD_GATE_API": "https://api.example"})
        assert res.exit_code == 0 and seen[0][1].startswith(f"https://api.example/v1/orgs/{ORG}/commits/{head}")

    def test_a_bad_commit_or_api_fails(self, repo, monkeypatch):
        assert "not a full commit id" in _gate(repo, monkeypatch, "--commit", "no-such-ref").output
        res = _gate(repo, monkeypatch, "--commit", "HEAD", "--api", "http://api.example")
        assert res.exit_code == 1 and "https://" in res.output

    def test_strict_is_a_deprecated_no_op(self, repo, monkeypatch):
        base = _git(repo, "rev-parse", "HEAD")
        head = _commit(repo, "change")
        _serve(monkeypatch, [(200, _verdict("none", sha=head))])
        res = _gate(repo, monkeypatch, "--commit", head, "--base", base, "--strict")
        assert res.exit_code == 0 and "deprecated" in res.output


# --- this runner's own evidence (Platform says none) ----------------------------

ENTRY = {"receipt_id": "rcpt_" + "1" * 32, "run_id": "run-1", "timestamp": "2026-10-18T10:00:00Z",
         "task": "change a", "executor": "claude-code"}


def _local_ci_pass(repo: Path) -> None:
    path = repo / ".openshard" / "runs.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ENTRY) + "\n", encoding="utf-8")
    sha = _git(repo, "rev-parse", "HEAD")
    runs = [{"name": "tests", "head_sha": sha, "status": "completed", "conclusion": "success",
             "started_at": "2026-10-18T12:00:00Z"}]
    item = ci.build_ci_attestation(ENTRY, ci.classify_check_runs(runs, sha), ci.CITarget(sha=sha, binding="clean_head"),
                                   created_at="2026-10-18T12:00:01Z")
    assert item is not None
    ps.record_attestation(repo, item)


class TestLocalEvidence:
    """Under ``all_prs`` a human change fails, so a pass can only come from trusted local evidence."""

    def _run(self, repo, monkeypatch):
        head = _git(repo, "rev-parse", "HEAD")
        _serve(monkeypatch, [(200, _verdict("none", sha=head, policy="all_prs"))])
        return json.loads(_gate(repo, monkeypatch, "--commit", head, "--base", head, "--json").output)

    def test_trusted_local_evidence_counts(self, repo, monkeypatch):
        _local_ci_pass(repo)
        body = self._run(repo, monkeypatch)
        assert (body["status"], body["local_verdict"]) == ("pass", "verified")

    def test_committed_symlinked_history_is_not_evidence(self, repo, monkeypatch):
        # The #626 reviewer's bypass: .openshard -> evil/, both committed.
        _local_ci_pass(repo)
        (repo / ".openshard").rename(repo / "evil")
        (repo / ".openshard").symlink_to("evil", target_is_directory=True)
        _git(repo, "add", "-f", ".openshard", "evil")
        _git(repo, "commit", "-q", "-m", "evidence")
        _local_ci_pass(repo)
        body = self._run(repo, monkeypatch)
        assert (body["status"], body["local_verdict"]) == ("fail", None)

    def test_an_untracked_symlinked_history_dir_is_not_evidence(self, repo, monkeypatch):
        _local_ci_pass(repo)
        (repo / ".openshard").rename(repo / "elsewhere")
        (repo / ".openshard").symlink_to("elsewhere", target_is_directory=True)
        assert self._run(repo, monkeypatch)["local_verdict"] is None

    def test_a_symlinked_evidence_file_is_not_evidence(self, repo, monkeypatch):
        _local_ci_pass(repo)
        real = repo / ".openshard" / "verifications.jsonl"
        real.rename(repo / "v.jsonl")
        real.symlink_to(repo / "v.jsonl")
        assert self._run(repo, monkeypatch)["local_verdict"] is None

    def test_a_committed_case_variant_history_dir_is_not_evidence(self, repo, monkeypatch):
        variant = repo / ".OpenShard"
        variant.mkdir()
        (variant / "runs.jsonl").write_text("{}\n", encoding="utf-8")
        _git(repo, "add", "-f", ".OpenShard")
        _git(repo, "commit", "-q", "-m", "variant")
        _local_ci_pass(repo)
        assert self._run(repo, monkeypatch)["local_verdict"] is None
