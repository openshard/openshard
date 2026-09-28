"""Audit regression: every human-facing surface makes the same, evidence-bounded claim.

Scenario (the hostile-audit case, driven through the real Cursor hook fold):

1. a file is already dirty before the session (excluded, never counted)
2. the agent writes a file
3. the agent reports ``python -m pytest`` with ``exitCode: 0`` -- OpenShard
   never ran pytest (a forged / unverified pass)
4. the agent runs ``rm -rf /`` which exits 1, delivered through the *success*
   hook (``postToolUse``), not ``postToolUseFailure``
5. ``openshard verify`` runs the repository's own check, which exits 2
6. the stored Receipt is edited by hand (integrity tampering)
7. the checksum is recomputed over the edited record

At each step the surfaces -- receipt rows, ``last``, ``last --json``,
``proof last``, ``trust last``, the Home screen, ``stats routing`` -- must
never claim more than the evidence supports, and must agree with each other
because they all read ``history/verification_truth``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.adapters.claude_hooks import handle_hook
from openshard.cli.main import cli
from openshard.history.shard_contract import (
    build_shard_receipt,
    render_compact_shard_receipt,
    verified_label,
)
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.verification_truth import (
    STATE_AGENT_REPORTED_FAILED,
    STATE_AGENT_REPORTED_PASSED,
    STATE_ATTEMPTED_UNVERIFIED,
    STATE_NOT_OBSERVED,
    STATE_NOT_RUN,
    STATE_VERIFIED_FAILED,
    STATE_VERIFIED_PASSED,
    interpret_evidence,
    interpret_receipt,
)
from openshard.verification.post_session import latest_for_entry, load_attestations
from tests.capture_fixtures import _make_repo

SID = "6f1a2b3c-4d5e-4f60-8a71-b2c3d4e5f607"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz012345"


def _doc(repo: Path, event: str, **fields) -> dict:
    base = {
        "conversation_id": SID, "generation_id": "g1", "hook_event_name": event, "model": "claude-4-sonnet",
        "cursor_version": "1.7.0", "workspace_roots": [str(repo)], "user_email": "dev@example.com",
        "transcript_path": "/home/u/.cursor/t.jsonl",
    }
    base.update(fields)
    return base


def _run(repo: Path, event: str, **fields) -> None:
    handle_hook(_doc(repo, event, **fields), env={}, agent="cursor")


def _runs(repo: Path) -> list[dict]:
    path = repo / ".openshard" / "runs.jsonl"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _rewrite_last(repo: Path, mutate) -> None:
    path = repo / ".openshard" / "runs.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[-1])
    mutate(entry)
    lines[-1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def audit_repo(tmp_path: Path, monkeypatch) -> Path:
    repo = _make_repo(tmp_path / "audit repo")
    (repo / "fail.py").write_text("import sys\nsys.exit(2)\n", encoding="utf-8")
    (repo / ".openshard").mkdir()
    (repo / ".openshard" / "config.yml").write_text(
        "verification_commands:\n  - " + json.dumps([sys.executable, "fail.py"]) + "\n", encoding="utf-8",
    )
    # 1. pre-existing dirty file
    (repo / "README.md").write_text("hello dirty\n", encoding="utf-8")
    _run(repo, "sessionStart", session_id=SID, is_background_agent=False)
    _run(repo, "beforeSubmitPrompt", prompt=f"Add calc; mail me at dev@example.com, token {JWT}")
    # 2. agent file write
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _run(repo, "postToolUse", tool_name="Write", tool_use_id="c1", cwd=str(repo),
         tool_input={"path": str(repo / "calc.py")}, tool_output=json.dumps({"ok": True}))
    _run(repo, "afterFileEdit", file_path=str(repo / "calc.py"), edits=[])
    # 3. forged agent-reported test pass
    _run(repo, "postToolUse", tool_name="Shell", tool_use_id="c2", cwd=str(repo),
         tool_input={"command": "python -m pytest -q"}, tool_output=json.dumps({"exitCode": 0, "stdout": "1 passed"}))
    # 4. failed dangerous command through the success hook
    _run(repo, "postToolUse", tool_name="Shell", tool_use_id="c3", cwd=str(repo),
         tool_input={"command": "rm -rf /"}, tool_output=json.dumps({"exitCode": 1, "stdout": "denied"}))
    _run(repo, "stop", status="completed", loop_count=0)
    _run(repo, "sessionEnd", session_id=SID, reason="completed", duration_ms=1000,
         is_background_agent=False, final_status="completed")
    monkeypatch.chdir(repo)
    return repo


def _surfaces(runner: CliRunner) -> dict:
    """One dict of what each surface currently claims."""
    last = runner.invoke(cli, ["last"], catch_exceptions=False)
    last_json = json.loads(runner.invoke(cli, ["last", "--json"], catch_exceptions=False).output)
    proof = runner.invoke(cli, ["proof", "last"], catch_exceptions=False)
    proof_json_res = runner.invoke(cli, ["proof", "last", "--json"], catch_exceptions=False)
    trust = runner.invoke(cli, ["trust", "last"], catch_exceptions=False)
    trust_json = json.loads(runner.invoke(cli, ["trust", "last", "--json"], catch_exceptions=False).output)
    home = runner.invoke(cli, [], catch_exceptions=False)
    routing = runner.invoke(cli, ["stats", "routing"], catch_exceptions=False)
    return {
        "last": last.output, "last_json": last_json, "proof": proof.output,
        "proof_json": json.loads(proof_json_res.output), "proof_exit": proof_json_res.exit_code,
        "trust": trust.output, "trust_json": trust_json, "home": home.output, "routing": routing.output,
    }


def _verify_cell(home_output: str) -> str:
    """The Verify cell of the single receipt row on the Home screen."""
    for line in home_output.splitlines():
        if "Add calc" in line:
            return line.split()[-2] if line.rstrip().endswith("│") else line.split()[-1]
    raise AssertionError("receipt row not rendered on Home")


class TestScenarioSurfacesAgree:
    def test_forged_pass_is_never_a_verified_pass(self, audit_repo):
        entry = _runs(audit_repo)[-1]
        block = entry["verification"]
        # Stored honestly: the agent's claim, labelled as such.
        assert block["source"] == "agent_reported" and block["status"] == "passed"

        s = _surfaces(CliRunner())
        truth = s["last_json"]["verification_truth"]
        assert truth["state"] == STATE_AGENT_REPORTED_PASSED
        assert truth["authority"] == "agent_reported" and truth["effective_status"] == "unknown"
        # Receipt rows: the claim stays visible, the Verified row denies verification.
        assert "1/1 passed (agent-reported)" in s["last"]
        assert "Verified    Not verified by OpenShard (agent reported 1/1 passed)" in s["last"]
        # Proof: verification is weak proof named for what it is, never "passed".
        verif = next(x for x in s["proof_json"]["proof_contract"]["sections"] if x["name"] == "verification")
        assert (verif["status"], verif["detail"]) == ("partial", STATE_AGENT_REPORTED_PASSED)
        assert s["proof_json"]["proof_contract"]["overall_status"] != "strong"
        # Trust: no "Verification passed", and the unverified penalty applies.
        assert "Verification passed" not in s["trust"]
        assert "the agent reported a pass; OpenShard did not run it" in s["trust"]
        assert "verification_unverified" in {p["code"] for p in s["trust_json"]["penalties"]}
        assert s["trust_json"]["signals"]["verification_state"] == STATE_AGENT_REPORTED_PASSED
        # Quality sentence and Home agree.
        assert "agent reported a pass" in s["last_json"]["shard_quality"]["summary"]
        assert "verification passed" not in s["last_json"]["shard_quality"]["summary"]
        assert _verify_cell(s["home"]) == "reported"
        # Routing statistics never counted it as verified either.
        assert "verified known: 0" in s["routing"]
        # Every JSON surface carries the same effective token.
        assert s["last_json"]["run"]["verification"]["status"] == "unknown"
        assert s["last_json"]["shard_quality"]["verification"] == "unknown"
        assert s["trust_json"]["signals"]["verification"] == "unknown"

    def test_failed_dangerous_command_is_recorded_activity_evidence(self, audit_repo):
        entry = _runs(audit_repo)[-1]
        cap = entry["capture"]
        # The success hook carried a non-zero exit: counted as an activity
        # failure, never as a tool-failure event and never as a check.
        assert cap["command_failure_count"] == 1 and cap["tool_failure_count"] == 0
        rm = next(e for e in entry["events"] if e["action"] == "Shell: rm -rf /")
        assert rm["status"] == "failed" and rm["metadata"]["exit_code"] == 1
        assert rm["metadata"]["command_safety"] == "blocked"  # existing policy classifier, not a new one
        assert entry["verification"]["checks_attempted"] == 1  # the rm is not a verification check
        assert "1 command(s) exited non-zero" in entry["summary"]
        out = render_compact_shard_receipt(build_shard_receipt(entry))
        assert "Shell × 2 (1 failed)" in out
        assert "Shell: rm -rf /  (exit 1; agent-reported; policy class: blocked)" in out

    def test_prompt_excerpt_redacts_email_and_jwt(self, audit_repo):
        task = _runs(audit_repo)[-1]["task"]
        assert "dev@example.com" not in task and JWT not in task
        assert "[email]" in task and "[jwt]" in task

    def test_openshard_executed_failure_takes_precedence_everywhere(self, audit_repo):
        runner = CliRunner()
        res = runner.invoke(cli, ["verify", "--approve"], catch_exceptions=False)
        assert res.exit_code == 0, res.output  # default exit behaviour unchanged
        assert "failed (exit 2)" in res.output
        entry = _runs(audit_repo)[-1]
        # The Receipt bytes are untouched; the attestation lives beside them.
        assert entry["verification"]["status"] == "passed" and entry["verification"]["source"] == "agent_reported"
        post = latest_for_entry(entry, load_attestations(audit_repo / ".openshard"))
        assert post is not None and post["verification"]["status"] == "failed"

        s = _surfaces(runner)
        truth = s["last_json"]["verification_truth"]
        assert truth["state"] == STATE_VERIFIED_FAILED and truth["basis"] == "post_session"
        assert truth["claim_status"] == "passed" and truth["claim_source"] == "agent_reported"
        assert "Re-verified: 0/1 passed" in s["last"]
        assert "Verified    Failed (OpenShard re-ran the check(s): 0/1 passed; not bound to a commit); " \
               "the agent had reported passed" in s["last"]
        verif = next(x for x in s["proof_json"]["proof_contract"]["sections"] if x["name"] == "verification")
        assert (verif["status"], verif["detail"]) == ("present", "failed")
        assert "Verification failed (OpenShard-observed)" in s["trust"]
        assert "verification_failed" in {p["code"] for p in s["trust_json"]["penalties"]}
        assert s["trust_json"]["score"] < 60
        assert "verification failed on OpenShard re-run" in s["last_json"]["shard_quality"]["summary"]
        assert _verify_cell(s["home"]) == "failed"
        for token in (
            s["last_json"]["run"]["verification"]["status"], s["last_json"]["shard_quality"]["verification"],
            s["trust_json"]["signals"]["verification"],
        ):
            assert token == "failed"

    def test_tampered_record_is_never_a_positive_assessment(self, audit_repo):
        runner = CliRunner()
        runner.invoke(cli, ["verify", "--approve"], catch_exceptions=False)

        def tamper(e: dict) -> None:
            e["summary"] = "TAMPERED: everything passed"
            e["files_updated"] = 99

        _rewrite_last(audit_repo, tamper)
        s = _surfaces(runner)
        assert "Checksum mismatch" in s["last"] and "Proof: unsafe" in s["last"]
        assert "no longer matches its checksum" in s["last"]
        assert s["last_json"]["content_hash_status"] == "mismatch"
        assert s["last_json"]["proof_contract"]["overall_status"] == "unsafe"
        assert "content_hash_mismatch" in s["last_json"]["proof_contract"]["unsafe_findings"]
        assert s["proof_exit"] == 1 and "Status: unsafe" in s["proof"]
        assert "no longer matches its checksum" in s["proof"]
        assert s["trust_json"]["score"] == 0 and s["trust_json"]["band"] == "unsafe"
        assert "integrity_mismatch" in {p["code"] for p in s["trust_json"]["penalties"]}
        assert "no longer matches its checksum" in s["trust"]
        assert _verify_cell(s["home"]) == "edited"

    def test_recomputed_checksum_matches_and_wording_never_claims_authorship(self, audit_repo):
        def tamper(e: dict) -> None:
            e["summary"] = "TAMPERED"

        _rewrite_last(audit_repo, tamper)

        def rehash(e: dict) -> None:
            e["content_hash"] = compute_shard_hash(e)

        _rewrite_last(audit_repo, rehash)
        s = _surfaces(CliRunner())
        # An unkeyed checksum cannot tell a recomputed hash from the original;
        # the wording therefore only ever claims a match, never authorship.
        assert s["last_json"]["content_hash_status"] == "valid"
        assert "Checksum matches" in s["last"]
        full = CliRunner().invoke(cli, ["last", "--full"], catch_exceptions=False).output
        assert "does not prove who wrote it" in full
        for text in (s["last"], full, s["proof"], s["trust"]):
            low = text.lower()
            assert "signed" not in low and "signature" not in low and "authentic" not in low


class TestVerifyStrict:
    def test_strict_exit_codes(self, audit_repo):
        runner = CliRunner()
        # Default: evidence only, exit 0 on a failed check.
        assert runner.invoke(cli, ["verify", "--approve"]).exit_code == 0
        # Strict: a failed executed check exits 1.
        res = runner.invoke(cli, ["verify", "--approve", "--strict"])
        assert res.exit_code == 1 and "Strict: an executed check failed" in res.output
        # Strict without approval: the planned check could not run -> 2.
        res = runner.invoke(cli, ["verify", "--strict"])
        assert res.exit_code == 2 and "could not run" in res.output
        res = runner.invoke(cli, ["verify", "--strict", "--json"])
        assert res.exit_code == 2
        assert json.loads(res.output)["exit_code"] == 2
        # A passing contract exits 0 under --strict.
        (audit_repo / ".openshard" / "config.yml").write_text(
            "verification_commands:\n  - " + json.dumps([sys.executable, "-c", "pass"]) + "\n", encoding="utf-8",
        )
        assert runner.invoke(cli, ["verify", "--approve", "--strict"]).exit_code == 0
        # Nothing planned at all -> 2.
        (audit_repo / ".openshard" / "config.yml").write_text("{}\n", encoding="utf-8")
        assert runner.invoke(cli, ["verify", "--strict"]).exit_code == 2
        assert runner.invoke(cli, ["verify"]).exit_code == 0


class TestInterpretationRules:
    def _block(self, **kw) -> dict:
        base = {"version": 1, "observation_mode": "hook_tool_event", "checks": [], "checks_attempted": 1,
                "checks_passed": 1, "checks_failed": 0, "checks_skipped": 0}
        base.update(kw)
        return base

    def test_agent_reported_pass_is_unknown_but_labelled(self):
        t = interpret_evidence(self._block(source="agent_reported", status="passed"))
        assert (t.state, t.effective_status, t.authority) == (STATE_AGENT_REPORTED_PASSED, "unknown", "agent_reported")
        assert t.to_dict()["label"] == "Not verified by OpenShard (agent reported 1/1 passed)"

    def test_agent_reported_failure_stays_failed(self):
        t = interpret_evidence(self._block(source="agent_reported", status="failed", checks_passed=0, checks_failed=1))
        assert (t.state, t.effective_status) == (STATE_AGENT_REPORTED_FAILED, "failed")

    def test_observed_outcomes_pass_through(self):
        for src in ("directly_observed", "git_verified", "independently_verified"):
            t = interpret_evidence(self._block(source=src, status="passed", observation_mode="openshard_executed"))
            assert (t.state, t.effective_status, t.authority) == (STATE_VERIFIED_PASSED, "passed", src)

    def test_post_session_precedence_and_no_override_when_nothing_ran(self):
        session = self._block(source="agent_reported", status="passed")
        failed_rerun = {"verification": {
            "version": 1, "source": "directly_observed", "observation_mode": "openshard_executed",
            "status": "failed", "checks": [{"name": "pytest", "kind": "test", "status": "failed", "exit_code": 2}],
            "checks_attempted": 1, "checks_passed": 0, "checks_failed": 1, "checks_skipped": 0,
            "artifact_sha": "a" * 40,
        }}
        t = interpret_evidence(session, post_session_verification=failed_rerun)
        assert (t.state, t.effective_status, t.basis) == (STATE_VERIFIED_FAILED, "failed", "post_session")
        assert t.claim_status == "passed" and "@ aaaaaaaaaaaa" in t.to_dict()["label"]
        nothing_ran = {"verification": {"version": 1, "source": "directly_observed",
                                        "observation_mode": "openshard_executed", "status": "not_run"}}
        t = interpret_evidence(session, post_session_verification=nothing_ran)
        assert t.state == STATE_AGENT_REPORTED_PASSED and t.basis == "session"
        # A block in the sidecar that is not OpenShard-executed can never be promoted.
        forged = {"verification": {"version": 1, "source": "agent_reported", "observation_mode": "agent_claim",
                                   "status": "passed"}}
        t = interpret_evidence(session, post_session_verification=forged)
        assert t.state == STATE_AGENT_REPORTED_PASSED

    def test_weak_states(self):
        t = interpret_evidence(self._block(source="directly_observed", status="unknown", checks_passed=0,
                                           incomplete_reasons=["outcome_not_observed"]))
        assert t.state == STATE_ATTEMPTED_UNVERIFIED and t.effective_status == "unknown"
        t = interpret_evidence(self._block(source="directly_observed", status="not_run", checks_attempted=0,
                                           checks_passed=0))
        assert t.state == STATE_NOT_RUN and t.effective_status == "not_run"
        t = interpret_evidence(None)
        assert t.state == STATE_NOT_OBSERVED and t.effective_status == "unknown"
        t = interpret_evidence(None, legacy_status="Passed")
        assert t.state == STATE_VERIFIED_PASSED  # hand-built receipts keep the old mapping

    def test_receipt_and_cli_use_the_same_function(self):
        entry = {"task": "t", "timestamp": "2026-01-01T00:00:00Z", "executor": "native",
                 "verification_attempted": True, "verification_passed": True}
        receipt = build_shard_receipt(entry)
        assert interpret_receipt(receipt).state == STATE_VERIFIED_PASSED
        assert verified_label(receipt) == "Passed (OpenShard ran the check(s))"


class TestFirstRunAndSmallBugs:
    def test_home_does_not_claim_configured_without_user_config(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "fresh")
        monkeypatch.chdir(repo)
        for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "OPENSHARD_CONFIG"):
            monkeypatch.delenv(var, raising=False)
        runner = CliRunner()
        home = runner.invoke(cli, [], catch_exceptions=False).output
        assert "Mode:  Not configured" in home and "Claude Sonnet" not in home
        doctor = runner.invoke(cli, ["doctor"], catch_exceptions=False).output
        assert "config found: no" in doctor
        agent = json.loads(runner.invoke(cli, ["setup", "--agent"], catch_exceptions=False).output)
        assert agent["config_found"] is False
        # With a real config file, Home says Configured and names that model.
        (repo / ".openshard").mkdir()
        (repo / ".openshard" / "config.yml").write_text("execution_model: anthropic/claude-sonnet-4.6\n")
        home = runner.invoke(cli, [], catch_exceptions=False).output
        assert "Mode:  Configured" in home and "Claude Sonnet 4.6" in home

    def test_no_color_is_not_an_agent_environment(self, monkeypatch, tmp_path):
        from openshard.config.settings import is_agent_environment, load_config

        for var in ("OPENSHARD_AGENT", "CI", "GITHUB_ACTIONS", "GITLAB_CI", "OPENSHARD_CONFIG"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.chdir(tmp_path)
        assert is_agent_environment() is False
        assert load_config().get("output_mode") != "agent_json"
        out = CliRunner().invoke(cli, ["env"], catch_exceptions=False).output
        assert "Agent mode:    no" in out and "NO_COLOR" not in out

    def test_setup_renders_antigravity_once(self):
        from openshard.cli.main import _render_setup_result

        class _R:
            status = "skipped"
            message = ""

        class _Result:
            is_git = True
            history_writable = True
            readiness = "not_ready"
            next_steps: list[str] = []
            mcp = hooks = statusline = capture_service = None
            agents = {k: _R() for k in ("codex", "opencode", "cursor", "antigravity", "grok_build", "hermes")}

            class claude_cli:
                available = False

            def configured_agents(self):
                return []

        runner = CliRunner()
        with runner.isolation() as (out, _err, _fn):
            _render_setup_result(_Result())
            text = out.getvalue().decode("utf-8")
        assert text.count("Antigravity:") == 1

    def test_capture_profiles_no_longer_say_verification_is_never_recorded(self):
        from openshard.adapters.capture_agents import AGENT_PROFILES

        for profile in AGENT_PROFILES.values():
            assert "never recorded" not in profile.import_note
            assert "agent_reported" in profile.import_note

    def test_demo_copy_does_not_overclaim(self):
        out = CliRunner().invoke(cli, ["demo", "shard"], catch_exceptions=False).output
        assert "safe to rely on" not in out
        assert "not a safety guarantee" in out
        assert "Verification: Passed (OpenShard ran the check(s))" in out


class TestRedaction:
    def test_conservative_patterns(self):
        from openshard.security.redaction import redact_sensitive_text

        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
        text, kinds = redact_sensitive_text(f"ping ops@corp.example, key {JWT} and {pem} done")
        assert text == "ping [email], key [jwt] and [private-key] done"
        assert kinds == ["private_key", "jwt", "email"]
        # Truncated PEM (capped excerpt) is still redacted to the end.
        text, _ = redact_sensitive_text("x -----BEGIN PRIVATE KEY-----\nMIIE...")
        assert text == "x [private-key]"
        # No false positives on ordinary text, versions, paths or decorators.
        for benign in ("run pytest -q", "v1.2.3 @ 10:00", "user@host style is not an address",
                       "see docs/api.md", "a.b@c", "eyJ short.tok.en", "git log --author=me"):
            assert redact_sensitive_text(benign) == (benign, [])

    def test_command_text_is_redacted(self):
        from openshard.adapters.claude_hooks import summarize_command

        action, _target, _kind = summarize_command("git commit --author='Dev <dev@example.com>' -m fix")
        assert "dev@example.com" not in action and "[email]" in action


class TestStateFilePermissions:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_capture_state_and_telemetry_state_are_owner_only(self, tmp_path, monkeypatch):
        from openshard.adapters.claude_capture_service import _write_state
        from openshard.telemetry import state as tstate

        path = tmp_path / "claude-capture.json"
        _write_state(path, {"recent_repos": [str(tmp_path)]})
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        monkeypatch.setenv("OPENSHARD_HOME", str(tmp_path / "home"))
        st, _created = tstate.ensure_state(dict(os.environ))
        assert oct(Path(tstate.state_path(dict(os.environ))).stat().st_mode & 0o777) == "0o600"


class TestHistoricalReceiptIntegrityIsNeverManufactured:
    """A record that never stored a content hash stays "integrity not recorded" on every read path."""

    ENTRY = {
        "schema_version": "1.2", "timestamp": "2025-01-01T00:00:00Z", "task": "old run", "executor": "native",
        "verification_attempted": True, "verification_passed": True, "files_updated": 1,
        "files_detail": [{"path": "a.py", "change_type": "update"}], "summary": "done",
    }

    def test_proof_contract_reads_the_record_without_stamping_a_hash(self):
        from unittest.mock import patch

        from openshard.history import proof_contract as pc

        entry = json.loads(json.dumps(self.ENTRY))
        before = json.dumps(entry, sort_keys=True)
        assert build_shard_receipt(entry).integrity_status == "missing"
        seen: dict = {}
        real = pc.build_shard_receipt

        def spy(e, **kw):
            receipt = real(e, **kw)
            seen["integrity_status"] = receipt.integrity_status
            seen["hash_in_coerced"] = "content_hash" in e
            return receipt

        with patch.object(pc, "build_shard_receipt", spy):
            contract = pc.build_shard_proof_contract(entry)
        assert seen == {"integrity_status": "missing", "hash_in_coerced": False}
        assert "content_hash_mismatch" not in contract["unsafe_findings"]
        assert json.dumps(entry, sort_keys=True) == before  # never mutated

    def test_stored_hash_is_still_checked_not_restamped(self):
        entry = json.loads(json.dumps(self.ENTRY))
        entry["content_hash"] = compute_shard_hash(entry)
        entry["summary"] = "edited after the hash was written"
        from openshard.history.proof_contract import build_shard_proof_contract

        assert build_shard_receipt(entry).integrity_status == "mismatch"
        assert "content_hash_mismatch" in build_shard_proof_contract(entry)["unsafe_findings"]

    def test_cli_surfaces_say_not_recorded_for_a_hashless_record(self, tmp_path, monkeypatch):
        repo = _make_repo(tmp_path / "legacy")
        (repo / ".openshard").mkdir()
        (repo / ".openshard" / "runs.jsonl").write_text(json.dumps(self.ENTRY) + "\n", encoding="utf-8")
        monkeypatch.chdir(repo)
        runner = CliRunner()
        last = runner.invoke(cli, ["last"], catch_exceptions=False).output
        assert "Integrity   Not recorded" in last and "Checksum" not in last
        last_json = json.loads(runner.invoke(cli, ["last", "--json"], catch_exceptions=False).output)
        assert last_json["content_hash_status"] == "missing" and last_json["content_hash"] is None
        assert last_json["verification_truth"]["integrity"] == "missing"
        assert last_json["proof_contract"]["unsafe_findings"] == []
        proof = runner.invoke(cli, ["proof", "last"], catch_exceptions=False).output
        assert "Integrity: Not recorded" in proof
        trust = json.loads(runner.invoke(cli, ["trust", "last", "--json"], catch_exceptions=False).output)
        assert trust["signals"]["integrity"] == "missing"
        assert "integrity_mismatch" not in {p["code"] for p in trust["penalties"]}
        # Reading never wrote a hash back into the stored record.
        stored = json.loads((repo / ".openshard" / "runs.jsonl").read_text(encoding="utf-8"))
        assert "content_hash" not in stored
