"""P1 Receipt contract additions: control / proof / cost evidence Core already stores.

Every new key is extended-projection only, present-and-null when the record
carries nothing, bounded, and free of paths. Nothing here is new capture.
"""

from __future__ import annotations

import json

from openshard.history import receipt_evidence as ev
from openshard.history.shard_contract import ShardReceipt, build_shard_receipt
from openshard.history.views import receipt_to_dict
from openshard.sync import envelope

NEW_KEYS = (
    "policy_decisions", "approval_detail", "sandbox_detail", "execution_loop", "base_commit",
    "content_hash", "session", "routing", "retry",
)
SHA = "c2dbd23"
HASH = "sha256:" + "a" * 64


def _entry(**extra) -> dict:
    e = {"receipt_id": "rcpt_" + "1" * 32, "timestamp": "2026-09-16T09:12:03Z", "task": "t",
         "agent": "codex", "schema_version": "1.2"}
    e.update(extra)
    return e


def _ext(entry: dict) -> dict:
    return receipt_to_dict(build_shard_receipt(entry, index=0), extended=True)


def _default(entry: dict) -> dict:
    return receipt_to_dict(build_shard_receipt(entry, index=0))


class TestOldRecords:
    def test_old_record_has_every_new_key_null_in_extended(self):
        d = _ext(_entry())
        for key in NEW_KEYS:
            assert key in d and d[key] is None, key

    def test_default_projection_is_unchanged(self):
        d = _default(_entry(git_head_commit_hash=SHA, retry_triggered=True, fixer_model="m/x",
                            stage_runs=[{"stage_type": "planning", "model": "a/b", "duration": 1.0, "cost": 0.1}]))
        assert not set(NEW_KEYS) & set(d)
        assert d["model_stages"] and all(set(s) == {"stage", "model"} for s in d["model_stages"])

    def test_previous_extended_keys_are_unchanged(self):
        keys = set(_ext(_entry()))
        old = keys - set(NEW_KEYS)
        assert {"receipt_id", "verification", "tokens_provenance", "cost_usd", "repo_identity", "model_stages"} <= old
        assert keys == old | set(NEW_KEYS)

    def test_model_stages_keep_previous_shape_without_metrics(self):
        d = _ext(_entry(stage_runs=[{"stage_type": "planning", "model": "anthropic/claude-sonnet-4.6"}]))
        assert d["model_stages"] and all(set(s) == {"stage", "model"} for s in d["model_stages"])

    def test_hand_built_receipt_without_evidence_projects_nulls(self):
        r = ShardReceipt(
            shard_id="s", created_at="2026-01-01T00:00:00Z", task_short="t", task_full="t", agent="a",
            strategy="", model_display="m", risk="", sandbox="", files_changed=0, checks_display="",
            approval="", cost_display="", result="", status="", duration_seconds=None,
        )
        d = receipt_to_dict(r, extended=True)
        assert all(d[k] is None for k in NEW_KEYS)

    def test_envelope_carries_the_new_keys_and_old_records_still_build(self):
        doc = envelope.build_envelope(_entry(), 0, core_version="0.4.9")
        assert set(NEW_KEYS) <= set(doc["receipt"])
        assert envelope.payload_hash(doc["receipt"]).startswith("sha256:")
        full = envelope.build_envelope(_entry(git_head_commit_hash=SHA), 0, core_version="0.4.9")
        assert full["receipt"]["base_commit"] == SHA
        json.dumps(full)


class TestPolicyDecisions:
    def _pd(self, **kw):
        d = {"decision_id": "abc", "action": "write_file", "resource": ".env", "decision": "deny",
             "reason": "secret path", "source": "path_policy", "severity": "high", "approval_required": False,
             "approval_granted": None, "scope": "repo", "created_at": "2026-06-17T09:22:21Z"}
        d.update(kw)
        return d

    def test_shape_and_privacy(self):
        d = _ext(_entry(policy_decisions=[self._pd()]))
        assert d["policy_decisions"] == [{
            "decision": "deny", "action": "write_file", "source": "path_policy", "severity": "high",
            "reason": "secret path", "approval_required": False, "approval_granted": None,
            "created_at": "2026-06-17T09:22:21Z",
        }]
        blob = json.dumps(d["policy_decisions"])
        assert "decision_id" not in blob and "resource" not in blob and "scope" not in blob and ".env" not in blob

    def test_malformed_values_dropped(self):
        entry = _entry(policy_decisions=[
            self._pd(decision="maybe"),
            self._pd(created_at="yesterday", approval_required="yes", reason="x" * 1000),
        ])
        [item] = _ext(entry)["policy_decisions"]
        assert item["created_at"] is None and item["approval_required"] is None
        assert len(item["reason"]) == 300

    def test_bounded_count_and_all_invalid_is_null(self):
        assert len(ev.policy_decisions_block([{"decision": "allow"}] * 50)) == ev.MAX_POLICY_DECISIONS
        assert ev.policy_decisions_block([{"decision": "nope"}, "x"]) is None
        assert ev.policy_decisions_block("junk") is None


class TestApprovalDetail:
    def test_shape_and_provenance_split(self):
        d = _ext(_entry(
            approval_request={"source": "change_budget", "requires_approval": True, "reason": "too many files",
                              "action": "ask", "proposed_files": 12, "budget_max_files": 5, "prompt": "SECRET PROMPT"},
            approval_receipt={"source": "cli_prompt", "requested": True, "granted": False, "action": "ask",
                              "reason": "user declined"},
        ))
        assert d["approval_detail"] == {
            "requires_approval": True, "request_source": "change_budget", "request_action": "ask",
            "request_reason": "too many files", "proposed_files": 12,
            "decision_source": "cli_prompt", "granted": False, "decision_action": "ask",
            "decision_reason": "user declined",
        }
        assert "SECRET PROMPT" not in json.dumps(d)

    def test_request_only_and_malformed(self):
        d = ev.approval_detail_block({"approval_request": {"requires_approval": "yes", "proposed_files": -3, "source": 5}})
        assert d is None
        d = ev.approval_detail_block({"approval_request": {"requires_approval": True}, "approval_receipt": {"granted": "no"}})
        assert d is not None and d["requires_approval"] is True and d["granted"] is None
        assert ev.approval_detail_block({"approval_request": {}, "approval_receipt": None}) is None


class TestSandboxDetail:
    def test_shape_never_sends_paths(self):
        d = _ext(_entry(sandbox={
            "sandbox_enabled": True, "sandbox_type": "worktree", "worktree_path": "C:/Users/me/wt",
            "worktree_branch": "osn/run-1", "fallback_reason": None, "safe_workspace_display_name": "wt-1",
        }))
        assert d["sandbox_detail"] == {"enabled": True, "type": "worktree", "fallback_reason": None}
        blob = json.dumps(d)
        assert "C:/Users" not in blob and "wt-1" not in blob

    def test_fallback_reason_bounded_and_junk_is_null(self):
        d = ev.sandbox_detail_block({"sandbox": {"sandbox_enabled": False, "sandbox_type": "temp", "fallback_reason": "r" * 999}})
        assert d is not None and len(d["fallback_reason"]) == 300
        assert ev.sandbox_detail_block({"sandbox": "on"}) is None
        assert ev.sandbox_detail_block({"sandbox": {}}) is None


class TestExecutionLoop:
    LOOP = {
        "status": "verified", "stop_reason": "verified", "verification_state": "passed",
        "sandbox_path": "C:/tmp/osn/work", "changed_files": ["a.py"],
        "attempts": [
            {"n": 1, "proposed": ["a.py", "<unsafe-path>"], "applied": ["a.py"], "blocked": ["<unsafe-path>"],
             "policy": {"allowed": ["a.py"]}, "verification": {"command": ["pytest"], "passed": False}},
            {"n": 2, "proposed": ["a.py"], "applied": ["a.py"], "blocked": [], "policy": {}, "verification": None},
        ],
        "evidence": {"actions": "agent_declared", "policy_and_file_effects": "openshard_observed",
                     "verification": "openshard_observed", "task_text_stored": False},
    }

    def test_counts_and_provenance_labels_only(self):
        d = _ext(_entry(osn_loop=self.LOOP))["execution_loop"]
        assert d == {
            "status": "verified", "stop_reason": "verified", "verification_state": "passed",
            "attempts": [
                {"n": 1, "proposed_count": 2, "applied_count": 1, "blocked_count": 1},
                {"n": 2, "proposed_count": 1, "applied_count": 1, "blocked_count": 0},
            ],
            "evidence": {"actions": "agent_declared", "policy_and_file_effects": "openshard_observed",
                         "verification": "openshard_observed"},
        }
        blob = json.dumps(d)
        assert "a.py" not in blob and "C:/tmp" not in blob and "pytest" not in blob

    def test_malformed_attempts_bounded(self):
        loop = {"status": "failed", "attempts": [{"n": "x"}, "junk", {"n": True}] + [{"n": i} for i in range(30)]}
        d = ev.execution_loop_block({"osn_loop": loop})
        assert d is not None and len(d["attempts"]) <= ev.MAX_LOOP_ATTEMPTS
        assert ev.execution_loop_block({"osn_loop": {"attempts": "bad"}}) is None
        assert ev.execution_loop_block({"osn_loop": []}) is None


class TestBaseCommitAndContentHash:
    def test_valid_values(self):
        d = _ext(_entry(git_head_commit_hash=SHA.upper(), content_hash=HASH))
        assert d["base_commit"] == SHA and d["content_hash"] == HASH

    def test_malformed_dropped(self):
        assert ev.base_commit_value({"git_head_commit_hash": "not-a-sha"}) is None
        assert ev.base_commit_value({"git_head_commit_hash": "abc"}) is None
        assert ev.base_commit_value({"git_head_commit_hash": 12345678}) is None
        assert ev.content_hash_value({"content_hash": "sha256:short"}) is None
        assert ev.content_hash_value({"content_hash": "a" * 64}) is None


class TestSession:
    CAPTURE = {
        "source": "claude_code_hooks", "session_id": "50bf9457-4f46-4cf0-8d3b-08dea904a3b8", "status": "ended",
        "session_end_observed": True, "session_end_reason": "other", "start_source": "startup",
        "started_at": "2026-09-03T01:09:08Z", "last_activity_at": "2026-09-03T17:13:06Z", "prompt_count": 2,
        "turn_count": 2, "tool_call_count": 4, "tool_failure_count": 0, "first_prompt_at": None,
        "last_turn_completed_at": None,
    }

    def test_shape_omits_session_id(self):
        d = _ext(_entry(capture=self.CAPTURE))["session"]
        assert d == {
            "started_at": "2026-09-03T01:09:08Z", "first_prompt_at": None, "last_turn_completed_at": None,
            "last_activity_at": "2026-09-03T17:13:06Z", "ended": True, "end_reason": "other",
            "start_source": "startup", "prompt_count": 2, "turn_count": 2, "tool_call_count": 4,
            "tool_failure_count": 0,
        }
        assert "50bf9457" not in json.dumps(d)

    def test_malformed_dropped(self):
        d = ev.session_block({"capture": {"started_at": "garbage", "prompt_count": -1, "tool_call_count": "4",
                                          "session_end_observed": "yes", "turn_count": 3}})
        assert d is not None and d["started_at"] is None and d["prompt_count"] is None
        assert d["tool_call_count"] is None and d["ended"] is None and d["turn_count"] == 3
        assert ev.session_block({"capture": {"started_at": "nope"}}) is None
        assert ev.session_block({"capture": "x"}) is None


class TestRouting:
    def test_no_recorded_routing_is_null(self):
        assert _ext(_entry(execution_model="deepseek/x"))["routing"] is None

    def test_scored_run_with_no_dispatch(self):
        d = _ext(_entry(execution_model="deepseek/deepseek-v4-pro", routing_selected_model="deepseek/deepseek-v4-pro",
                        routing_selected_provider="openrouter"))["routing"]
        assert d["mode"] == "scored" and d["selection_source"] == "deterministic"
        assert d["runtime_model"] == "deepseek/deepseek-v4-pro" and d["provider"] == "openrouter"
        assert d["role_dispatch_status"] == "not_dispatched" and d["roles"] is None
        assert d["fallback_used"] is None

    def test_tier_dispatch_roles_carry_dispatched_flags(self):
        tdr = {"enabled": True, "applied": True, "planner_model": "anthropic/claude-sonnet-4.6",
               "executor_model": "z-ai/glm-5.1", "validator_model": "anthropic/claude-sonnet-4.6",
               "planner_model_actual": "anthropic/claude-sonnet-4.6", "executor_model_actual": "z-ai/glm-5.1",
               "validator_dispatch_status": "skipped", "fallback_used": False, "fallback_reason": ""}
        d = _ext(_entry(execution_model="z-ai/glm-5.1", tier_dispatch_receipt=tdr))["routing"]
        assert d["mode"] == "tier_dispatch" and d["role_dispatch_status"] == "partially_dispatched"
        assert d["roles"] == [
            {"role": "planner", "model": "anthropic/claude-sonnet-4.6", "dispatched": True},
            {"role": "executor", "model": "z-ai/glm-5.1", "dispatched": True},
            {"role": "validator", "model": "anthropic/claude-sonnet-4.6", "dispatched": False},
        ]
        assert d["fallback_used"] is False and d["fallback_reason"] is None

    def test_advisory_role_models_are_not_dispatched(self):
        entry = _entry(execution_model="m/a", routing_selected_model="m/a",
                       model_candidate_scoring={"selected_by_role": {"planner": "frontier-reasoning-model"}})
        d = _ext(entry)["routing"]
        assert d["role_selection_mode"] == "advisory_only" and d["role_dispatch_status"] == "not_dispatched"
        assert d["roles"] == [{"role": "planner", "model": "frontier-reasoning-model", "dispatched": False}]

    def test_bounds(self):
        d = ev.routing_block(_entry(routing_selected_model="m/a", routing_selected_provider="p" * 500))
        assert d is not None and len(d["provider"]) == 128


class TestRetry:
    def test_shape(self):
        d = _ext(_entry(retry_triggered=True, fixer_model="anthropic/claude-sonnet-4.6",
                        retry_total_tokens=1234, retry_estimated_cost=0.0123))["retry"]
        assert d == {"triggered": True, "fixer_model": "anthropic/claude-sonnet-4.6",
                     "total_tokens": 1234, "cost_usd": 0.0123}

    def test_malformed_dropped_and_none_is_null(self):
        d = ev.retry_block({"retry_triggered": False, "retry_total_tokens": -5, "retry_estimated_cost": float("nan")})
        assert d == {"triggered": False, "fixer_model": None, "total_tokens": None, "cost_usd": None}
        assert ev.retry_block({"retry_triggered": "yes", "retry_estimated_cost": True}) is None
        assert ev.retry_block({}) is None


class TestModelStageMetrics:
    def test_duration_and_cost_added_only_when_recorded(self):
        d = _ext(_entry(stage_runs=[
            {"stage_type": "planning", "model": "anthropic/claude-sonnet-4.6", "duration": 5.68, "cost": 0.002253},
            {"stage_type": "implementation", "model": "z-ai/glm-5.1", "duration": "x", "cost": True},
            {"stage_type": "verification", "model": "m/v", "duration": 1.5},
        ]))["model_stages"]
        assert d[0]["duration_seconds"] == 5.68 and d[0]["cost_usd"] == 0.002253
        assert set(d[1]) == {"stage", "model"}
        assert d[2]["duration_seconds"] == 1.5 and "cost_usd" not in d[2]

    def test_observed_models_fallback_carries_no_metrics(self):
        d = _ext(_entry(capture={"models_seen": ["a/x", "b/y"]}))["model_stages"]
        assert len(d) == 2 and all(set(s) == {"stage", "model"} for s in d)


def test_projectors_never_raise_on_garbage():
    junk = {k: object() for k in ("approval_request", "approval_receipt", "sandbox", "osn_loop", "capture",
                                  "stage_runs", "tier_dispatch_receipt", "git_head_commit_hash", "content_hash",
                                  "retry_triggered", "fixer_model", "routing_selected_provider")}
    out = ev.project_entry_evidence(junk)
    assert out["approval_detail"] is None and out["session"] is None and out["model_stage_metrics"] == []
    assert ev.project_entry_evidence("not a dict") == {}


UNSAFE_TEXT = [
    "token: abc", "C:/Users/x/.env", r"C:\Users\x\.env", "/etc/passwd", r"\\server\share", "file:///tmp/x",
    "sk-abcdefgh12345", "AKIAABCDEFGH1234", "ghp_" + "a" * 20, "github_pat_" + "a" * 20, "xoxb-1234567890-abc",
    "-----BEGIN RSA PRIVATE KEY-----", "API_KEY=zzz", "password = hunter2", "Authorization: Bearer abc.def",
]
SAFE_TEXT = ["read-only task; writes not requested", "path_policy", "anthropic/claude-sonnet-4.6", "tokens exceeded budget"]


class TestPrivacyGuard:
    def test_helper_flags_paths_and_secrets_only(self):
        for text in UNSAFE_TEXT:
            assert ev.unsafe_text(text), text
        for text in SAFE_TEXT:
            assert not ev.unsafe_text(text), text

    def test_policy_reason_dropped_but_decision_kept(self):
        for bad in ("token: abc", "C:/Users/x/.env"):
            [item] = _ext(_entry(policy_decisions=[
                {"decision_id": "d", "decision": "deny", "action": "write_file", "source": "path_policy",
                 "severity": "high", "reason": bad},
            ]))["policy_decisions"]
            assert item["reason"] is None
            assert item["decision"] == "deny" and item["action"] == "write_file" and item["source"] == "path_policy"

    def test_every_free_text_leaf_is_guarded(self):
        bad = "C:/Users/x/secret.txt"
        entry = _entry(
            policy_decisions=[{"decision_id": "d", "decision": "ask", "action": bad, "source": bad,
                               "severity": bad, "reason": bad}],
            approval_request={"source": bad, "action": bad, "reason": bad, "requires_approval": True},
            approval_receipt={"source": bad, "action": bad, "reason": bad, "granted": True},
            sandbox={"sandbox_enabled": True, "sandbox_type": bad, "fallback_reason": bad},
            osn_loop={"status": bad, "stop_reason": bad, "verification_state": bad, "attempts": [{"n": 1}],
                      "evidence": {"actions": bad, "policy_and_file_effects": bad, "verification": bad}},
            capture={"session_end_reason": bad, "start_source": bad, "prompt_count": 1},
            routing_selected_model="m/a", routing_selected_provider=bad, execution_model=bad,
            tier_dispatch_receipt={"enabled": True, "applied": True, "planner_model": bad,
                                   "planner_model_actual": bad, "fallback_reason": bad},
            fixer_model=bad, retry_triggered=True,
        )
        d = _ext(entry)
        blocks = {k: d[k] for k in NEW_KEYS if k not in ("base_commit", "content_hash")}

        def leaves(node):
            if isinstance(node, dict):
                for v in node.values():
                    yield from leaves(v)
            elif isinstance(node, list):
                for v in node:
                    yield from leaves(v)
            elif isinstance(node, str):
                yield node

        assert not [s for s in leaves(blocks) if ev.unsafe_text(s)]
        assert d["policy_decisions"][0]["decision"] == "ask"
        assert d["approval_detail"]["granted"] is True and d["approval_detail"]["requires_approval"] is True
        assert d["sandbox_detail"]["enabled"] is True
        assert d["execution_loop"]["attempts"] == [{"n": 1, "proposed_count": 0, "applied_count": 0, "blocked_count": 0}]
        assert d["retry"] == {"triggered": True, "fixer_model": None, "total_tokens": None, "cost_usd": None}

    def test_no_forbidden_key_names_in_new_blocks(self):
        forbidden = {"prompt", "output", "env", "environment", "secret", "password", "diff", "patch", "timeline",
                     "messages", "stdout", "stderr", "transcript", "argv", "command", "path", "worktree_path",
                     "resource", "scope", "decision_id", "session_id"}
        d = _ext(_entry(
            policy_decisions=[{"decision_id": "d", "decision": "allow"}],
            approval_request={"requires_approval": True}, approval_receipt={"granted": True},
            sandbox={"sandbox_enabled": True, "sandbox_type": "temp"}, osn_loop=TestExecutionLoop.LOOP,
            capture=TestSession.CAPTURE, routing_selected_model="m/a", execution_model="m/a",
            retry_triggered=True, stage_runs=[{"stage_type": "planning", "model": "a/b", "duration": 1, "cost": 0.1}],
        ))

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)

        blocks = {k: d[k] for k in NEW_KEYS}
        assert not forbidden & set(keys(blocks))
