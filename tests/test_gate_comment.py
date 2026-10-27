"""The gate's "Proven / Not proven" summary and its one pull request comment (ci/gate_comment.py).

Pins: a check is proof only with a passed/failed outcome from a source the
Platform accepts; the agent's own report and a missing result are listed as
not proven; the comment is found by its marker and edited, never duplicated;
and nothing about posting it can change the gate's pass or fail.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from openshard.ci import gate_comment as gc
from openshard.ci.merge_gate import decide, render_proof

SHA = "a" * 40


def _check(name, outcome, source):
    return {"name": name, "outcome": outcome, "source": source}


class TestProof:
    def test_strong_sources_are_proof_and_the_agents_word_is_not(self):
        text = "\n".join(render_proof([{"checks": [
            _check("pytest", "passed", "directly_observed"),
            _check("ruff", "passed", "directly_observed"),
            _check("build", "failed", "independently_verified"),
            _check("eslint", "passed", "agent_reported"),
            _check("vitest", "unknown", "directly_observed"),
        ]}]))
        proven, not_proven = text.split("**Not proven**")
        assert "- 1 failed, reported by CI or another independent system: build" in proven
        assert "- 2 passed, seen directly by Openshard: pytest, ruff" in proven
        assert proven.index("1 failed") < proven.index("2 passed")  # failures first
        assert "eslint" not in proven and "vitest" not in proven
        assert "- 1 only the agent's own report: eslint (passed)" in not_proven
        assert "- 1 with no recorded result: vitest" in not_proven

    def test_nothing_proven_says_so(self):
        lines = render_proof([{"checks": [_check("pytest", "passed", "agent_reported")]}])
        assert "- Nothing: no check outcome here comes from Openshard, git or CI." in lines

    def test_no_checks_and_unsourced_checks(self):
        assert "- No checks were recorded." in render_proof([{"checks": []}])
        assert "- 1 with no recorded source: x (passed)" in render_proof([{"checks": [_check("x", "passed", None)]}])

    def test_long_lists_are_cut_and_names_are_escaped(self):
        checks = [_check(f"c{i}", "passed", "git_verified") for i in range(8)] + [
            _check("<img src=x>|`", "passed", "git_verified")]
        line = render_proof([{"checks": checks}])[2]
        assert line.startswith("- 9 passed, confirmed by Openshard against git: c0, c1, c2, c3, c4 and 4 more")
        assert "<img" not in "\n".join(render_proof([{"checks": checks[-1:]}]))

    def test_the_gate_summary_leads_with_it(self):
        answer = {"schema": "openshard.gate_verdict.v1", "sha": SHA, "status": "verified", "receipts": [
            {"receipt_id": "r1", "status": "verified", "checks": [_check("pytest", "passed", "directly_observed")]}],
            "policy": {"require_receipt": "agent_prs"}, "checked_at": "2026-10-27T10:00:00Z"}
        md = decide(SHA, answer).summary_markdown
        assert md.index("**Proven for this commit**") < md.index("#### Receipt")

    def test_no_receipts_means_no_proof_section(self):
        answer = {"status": "none", "receipts": [], "policy": {"require_receipt": "agent_prs"}}
        assert "Proven for this commit" not in decide(SHA, answer, agent_marks=[]).summary_markdown


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _GitHub:
    """A fake GitHub issues-comments API."""

    def __init__(self, comments=None, fail: int | None = None):
        self.comments = comments or []
        self.calls: list[tuple[str, str, dict | None]] = []
        self.fail = fail

    def __call__(self, req, timeout):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body))
        assert req.headers["Authorization"] == "Bearer tok"
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "no", {}, None)
        if req.get_method() == "GET":
            return _Resp(json.dumps(self.comments).encode())
        return _Resp(b"{}")


class TestUpsert:
    def test_first_run_posts_one_comment_with_the_marker(self):
        gh = _GitHub([{"id": 5, "body": "a human comment"}])
        ok, msg = gc.upsert("### Openshard gate · Passed", repo="o/r", number=7, token="tok", opener=gh)
        assert ok and "posted" in msg
        method, url, body = gh.calls[-1]
        assert (method, url) == ("POST", "https://api.github.com/repos/o/r/issues/7/comments")
        assert body["body"].startswith(gc.MARKER) and "Openshard gate · Passed" in body["body"]

    def test_later_runs_edit_the_same_comment(self):
        gh = _GitHub([{"id": 5, "body": "hi"}, {"id": 9, "body": gc.MARKER + "\nold"}])
        ok, msg = gc.upsert("new", repo="o/r", number=7, token="tok", opener=gh)
        assert ok and "updated" in msg
        assert gh.calls[-1][:2] == ("PATCH", "https://api.github.com/repos/o/r/issues/comments/9")
        assert [c[0] for c in gh.calls].count("POST") == 0

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_a_token_that_may_not_comment_is_a_note(self, code):
        ok, msg = gc.upsert("x", repo="o/r", number=7, token="tok", opener=_GitHub(fail=code))
        assert not ok and "pull-requests: write" in msg

    def test_bad_inputs_send_nothing(self):
        gh = _GitHub()
        assert gc.upsert("x", repo="o/r", number=7, token="", opener=gh)[0] is False
        assert gc.upsert("x", repo="../etc", number=7, token="tok", opener=gh)[0] is False
        assert gc.upsert("x", repo="o/r", number=7, token="tok", api="http://evil", opener=gh)[0] is False
        assert gh.calls == []

    def test_unreachable_is_a_note(self):
        def boom(req, timeout):
            raise urllib.error.URLError("down")
        assert gc.upsert("x", repo="o/r", number=7, token="tok", opener=boom)[0] is False

    def test_a_huge_summary_is_shortened_under_githubs_limit(self):
        assert len(gc.body_for("x" * 100_000)) < 65_536

    def test_pr_number(self):
        assert gc.pr_number({"pull_request": {"number": 12}}) == 12
        assert gc.pr_number({"merge_group": {}}) is None
        assert gc.pr_number({"pull_request": {"number": True}}) is None
