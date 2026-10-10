"""The read-only Receipt audit: honest counts, GET-only access, evidence-bound recovery."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("receipt_audit", Path(__file__).parents[1] / "scripts" / "receipt_audit.py")
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
sys.modules["receipt_audit"] = audit
_SPEC.loader.exec_module(audit)

SHA = "a" * 40


def _row(**receipt) -> dict:
    top = {k: receipt.pop(k) for k in ("usage_current", "verification_current", "origin", "source") if k in receipt}
    return {
        "receipt_id": receipt.pop("receipt_id", "rcpt_" + "1" * 32),
        "repo_identity": "github.com/openshard/openshard",
        "agent": receipt.get("agent"),
        "verification_status": receipt.get("verification_status"),
        "capture_completeness_status": "incomplete",
        "origin": top.get("origin", "github_observed"),
        "source": top.get("source", {"product": "openshard-github-cloud"}),
        "usage_current": top.get("usage_current"),
        "verification_current": top.get("verification_current"),
        "receipt": receipt,
    }


class TestTrailers:
    def test_claude_session_and_coauthor_paragraphs_both_count(self):
        message = (
            "Fixed the parser (#12)\n\nBody text: not a trailer block.\n\n"
            "Openshard-Agent: Claude Code\nOpenshard-Owner: Michael Obasa\n\n\n"
            "Claude-Session: https://claude.ai/code/session_01Hxo26gdbU2M9tzNECctDnN\n\n"
            "Co-authored-by: Claude Fable 5.1 <noreply@anthropic.com>"
        )
        evidence = audit.commit_evidence(SHA, message)
        assert evidence["declared"] == {"Openshard-Agent": "Claude Code", "Openshard-Owner": "Michael Obasa"}
        assert evidence["claude_session"] == "session_01Hxo26gdbU2M9tzNECctDnN"
        assert evidence["claude_coauthor_models"] == ["Fable 5.1"]
        assert evidence["pr_number"] == 12

    def test_body_lines_and_foreign_urls_are_not_evidence(self):
        message = "Subject\n\nMentions Openshard-Model: gpt in prose.\nClaude-Session: https://example.com/code/session_01Hxo26gdbU2M9tzNECctDnN"
        evidence = audit.commit_evidence(SHA, message)
        assert evidence["declared"] == {} and evidence["claude_session"] is None

    def test_scan_reads_every_ref(self):
        out = "\x00".join([SHA, "Subject\n\nOpenshard-Agent: Codex", "\x1e\n"])
        calls = []

        def run(*args):
            calls.append(args)
            return out

        (item,) = audit.scan_repository(Path("."), run=run)
        assert calls[0][:2] == ("log", "--all") and item["declared"] == {"Openshard-Agent": "Codex"}


class TestFacts:
    def test_later_usage_counts_and_sentinels_do_not(self):
        later = {"usage": {"model": {"id": "claude-fable-5-1"}, "tokens": {"status": "reported", "input": 3, "output": 4},
                           "cost": {"status": "estimated", "usd": 0.5}}}
        fact = audit.facts(_row(agent="Claude Code", model="unknown", usage_current=later))
        assert (fact["model"], fact["has_tokens"], fact["has_cost"]) == ("claude-fable-5-1", True, True)
        pending = {"usage": {"model": {}, "tokens": {"status": "pending", "input": 0}, "cost": {"status": "unknown", "usd": 0}}}
        fact = audit.facts(_row(agent="Codex", model="not recorded", usage_current=pending))
        assert (fact["model"], fact["has_tokens"], fact["has_cost"]) == (None, False, False)

    def test_generic_observation_labels_are_missing_agents_and_verification_uses_current_state(self):
        fact = audit.facts(_row(agent="GitHub observed cloud work", verification_status="unknown",
                                verification_current={"state": {"effective_status": "passed"}}))
        assert not fact["agent_known"] and fact["verification_complete"]
        assert not audit.facts(_row(agent="unknown", verification_status="not_run"))["verification_complete"]
        assert not audit.facts(_row(agent="OpenShard", origin="unknown"))["agent_known"]
        assert audit.facts(_row(agent="OpenShard", origin="openshard_routed"))["agent_known"]


class TestRecovery:
    def test_only_per_commit_declarations_recover_usage(self):
        fact = audit.facts(_row(agent="Claude Code", commit=SHA))
        evidence = audit.commit_evidence(SHA, "S\n\nOpenshard-Tokens-Input: 10\nOpenshard-Cost-USD: 0.2\n\nCo-authored-by: Claude Fable 5.1 <noreply@anthropic.com>")
        found = {c["field"]: c for c in audit.recovery_candidates(fact, evidence)}
        assert found["model"]["value"] == "claude-fable-5-1" and found["model"]["confidence"] == "high"
        assert found["tokens"]["value"] == {"Openshard-Tokens-Input": "10"}
        assert found["cost"]["value"] == "0.2"

    def test_coauthor_model_on_a_non_claude_receipt_needs_review(self):
        fact = audit.facts(_row(agent="Codex", commit=SHA))
        evidence = audit.commit_evidence(SHA, "S\n\nCo-authored-by: Claude Opus 5.5 <noreply@anthropic.com>")
        (candidate,) = audit.recovery_candidates(fact, evidence)
        assert candidate["confidence"] == "review"

    def test_claude_session_alone_is_a_claim_and_never_in_place(self):
        fact = audit.facts(_row(agent="GitHub observed cloud work", commit=SHA, model="m", tokens_input=1, cost_usd=1))
        evidence = audit.commit_evidence(SHA, "S\n\nClaude-Session: https://claude.ai/code/session_01Hxo26gdbU2M9tzNECctDnN")
        (candidate,) = audit.recovery_candidates(fact, evidence)
        assert (candidate["field"], candidate["confidence"], candidate["in_place"]) == ("agent", "claim", False)
        report = audit.build_report([_row(agent="GitHub observed cloud work", commit=SHA, model="m", tokens_input=1, cost_usd=1)],
                                    {SHA: evidence})
        assert report["totals"].get("eligible_for_recovery", 0) == 0 and report["totals"]["review_or_claim_only"] == 1

    def test_report_counts(self):
        rows = [
            _row(receipt_id="rcpt_a", agent="Codex", model="gpt", tokens_input=1, cost_usd=0.0, verification_status="passed"),
            _row(receipt_id="rcpt_b", agent="unknown", verification_status="unknown"),
        ]
        totals = audit.build_report(rows, {})["totals"]
        assert totals == {"total": 2, "with_model": 1, "with_tokens": 1, "with_cost": 1, "missing_agent": 1,
                          "incomplete_verification": 1, "incomplete_capture": 2, "with_missing_data": 1, "unrecoverable": 1}
        assert "| Total Receipts | 2 |" in audit.render_markdown(audit.build_report(rows, {}))


class TestReadOnlyClient:
    def test_pages_with_get_only_and_never_prints_the_key(self, capsys):
        seen = []
        pages = [{"receipts": [{"receipt_id": "rcpt_a"}], "next_cursor": "c1"}, {"receipts": [{"receipt_id": "rcpt_b"}], "next_cursor": None}]

        def opener(request, timeout):
            seen.append((request.get_method(), request.full_url, request.data))
            return io.BytesIO(json.dumps(pages.pop(0)).encode())

        client = audit.ReadOnlyClient("https://api.example.test", "org-1", "osk_secret", opener=opener)
        assert [r["receipt_id"] for r in client.receipts()] == ["rcpt_a", "rcpt_b"]
        assert all(method == "GET" and data is None for method, _, data in seen)
        assert "cursor=c1" in seen[1][1]
        assert "osk_secret" not in capsys.readouterr().out
        assert not hasattr(client, "post")

    def test_refuses_plain_http(self):
        with pytest.raises(ValueError):
            audit.ReadOnlyClient("http://api.example.test", "org", "k")

    def test_snapshot_refuses_inconsistent_listing(self):
        class Fake:
            def receipts(self):
                return [{"receipt_id": "rcpt_a"}]

            def get(self, path):
                return {"receipt_count": 2}

        with pytest.raises(SystemExit):
            audit.snapshot(Fake())


def _claude_line(ts: str, mid: str, inp: int, out: int) -> str:
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": mid, "model": "claude-fable-5-1", "role": "assistant",
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}})


class TestSessions:
    SID = "11111111-2222-3333-4444-555555555555"

    def _setup(self, tmp_path: Path, *, last_ts: str, segments: int = 1, ended: bool = True):
        claude = tmp_path / "claude"
        (claude / "proj").mkdir(parents=True)
        (claude / "proj" / f"{self.SID}.jsonl").write_text(
            _claude_line("2026-10-01T10:00:30Z", "m1", 5, 7) + "\n" + _claude_line(last_ts, "m2", 1, 2) + "\n", encoding="utf-8")
        history = tmp_path / "runs.jsonl"
        lines = []
        for i in range(segments):
            lines.append(json.dumps({"receipt_id": f"rcpt_{i}" + "0" * 30, "capture": {
                "session_id": self.SID, "started_at": "2026-10-01T10:00:00Z", "last_activity_at": "2026-10-01T10:05:00Z",
                "session_end_observed": ended}}))
        history.write_text("\n".join(lines) + "\n", encoding="utf-8")
        receipts = [_row(receipt_id="rcpt_0" + "0" * 30, agent="Claude Code (external)", origin="external_observed")]
        return audit.session_candidates(receipts, [history], claude, tmp_path / "codex")

    def test_single_ended_segment_inside_its_window_recovers_its_own_usage(self, tmp_path):
        (candidate,) = self._setup(tmp_path, last_ts="2026-10-01T10:04:00Z")
        assert candidate["confidence"] == "high"
        assert candidate["value"]["input"] == 6 and candidate["value"]["output"] == 9
        assert candidate["models"] == ["claude-fable-5-1"]

    def test_transcript_activity_after_the_receipt_is_not_attributed(self, tmp_path):
        (candidate,) = self._setup(tmp_path, last_ts="2026-10-01T11:00:00Z")
        assert candidate["confidence"] == "review" and "continues after" in candidate["evidence"]

    def test_multi_segment_sessions_are_never_split(self, tmp_path):
        (candidate,) = self._setup(tmp_path, last_ts="2026-10-01T10:04:00Z", segments=2)
        assert candidate["confidence"] == "review" and "several Receipt segments" in candidate["evidence"]
