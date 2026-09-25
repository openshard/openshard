from __future__ import annotations

import sys

import pytest

from openshard.osn.loop import FileWriteAction, LoopContext, run_bounded_loop

PY = sys.executable
CHECK = [PY, "-c", "import sys; sys.exit(0 if open('out.txt').read()=='ok' else 1)"]


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    (r / "out.txt").write_text("bad")
    return r


def test_verified_first_pass_and_repo_untouched(repo):
    rec = run_bounded_loop(repo, "t", lambda c: [FileWriteAction("out.txt", "ok")], CHECK)
    assert rec.status == "verified"
    assert rec.verification_state == "passed"
    assert len(rec.attempts) == 1
    assert (repo / "out.txt").read_text() == "bad"  # isolation
    assert rec.changed_files == ["out.txt"]


def test_retry_uses_failure_evidence_then_passes(repo):
    seen: list[LoopContext] = []

    def provider(ctx):
        seen.append(ctx)
        # First try changes the file but not to the right value; failure output
        # differs from nothing, so a retry is justified only via fingerprint.
        return [FileWriteAction("out.txt", "nope" if ctx.attempt == 1 else "ok")]

    rec = run_bounded_loop(repo, "t", provider, CHECK, max_attempts=3)
    assert rec.status == "verified"
    assert [a.n for a in rec.attempts] == [1, 2]
    assert rec.attempts[0].verification.passed is False
    assert seen[1].previous_failure is not None


def test_identical_failure_stops_retrying(repo):
    calls = []

    def provider(ctx):
        calls.append(ctx.attempt)
        return [FileWriteAction("out.txt", "still-bad")]

    rec = run_bounded_loop(repo, "t", provider, CHECK, max_attempts=5)
    # attempt 2 proposes identical writes -> stop before applying/verifying again
    assert rec.status == "failed"
    assert rec.stop_reason == "no_progress_identical_actions"
    assert calls == [1, 2]
    assert len(rec.attempts) == 1


def test_identical_failure_output_with_new_actions_stops(repo):
    rec = run_bounded_loop(
        repo, "t", lambda c: [FileWriteAction("out.txt", f"v{c.attempt}")], CHECK, max_attempts=5,
    )
    assert rec.stop_reason == "no_progress_identical_failure"
    assert len(rec.attempts) == 2


def test_max_attempts_bounded(repo):
    failing = [PY, "-c", "import sys, time; print(time.time_ns()); sys.exit(1)"]
    rec = run_bounded_loop(
        repo, "t", lambda c: [FileWriteAction("out.txt", str(c.attempt))], failing, max_attempts=99,
    )
    assert rec.status == "failed"
    assert rec.stop_reason == "max_attempts_exhausted"
    assert len(rec.attempts) == 5  # hard cap


def test_policy_block_is_not_retried_and_not_written(repo):
    rec = run_bounded_loop(
        repo, "t", lambda c: [FileWriteAction(".env", "S=1"), FileWriteAction("out.txt", "ok")], CHECK,
    )
    assert rec.status == "blocked"
    assert rec.verification_state == "not_run"  # never verified a blocked plan
    assert rec.attempts[0].blocked == [".env"]
    assert len(rec.attempts) == 1
    assert not (repo / ".env").exists()


def test_ask_path_without_approver_blocks(repo):
    rec = run_bounded_loop(repo, "t", lambda c: [FileWriteAction("pyproject.toml", "x")], CHECK)
    assert rec.status == "blocked"


def test_ask_path_with_approver_applies(repo):
    rec = run_bounded_loop(
        repo, "t",
        lambda c: [FileWriteAction("pyproject.toml", "x"), FileWriteAction("out.txt", "ok")],
        CHECK, approver=lambda p, d: (True, "test_approver"),
    )
    assert rec.status == "verified"
    assert rec.attempts[0].policy["approval_sources"] == ["test_approver"]


def test_unsafe_path_blocked(repo):
    rec = run_bounded_loop(repo, "t", lambda c: [FileWriteAction("../evil.txt", "x")], CHECK)
    assert rec.status == "blocked"
    assert not (repo.parent / "evil.txt").exists()


def test_no_actions_and_provider_error(repo):
    assert run_bounded_loop(repo, "t", lambda c: [], CHECK).status == "no_actions"

    def boom(c):
        raise RuntimeError("x")

    rec = run_bounded_loop(repo, "t", boom, CHECK)
    assert rec.status == "error"
    assert rec.verification_state == "not_run"


def test_unrunnable_verifier_is_failure_not_pass(repo):
    rec = run_bounded_loop(
        repo, "t", lambda c: [FileWriteAction("out.txt", "ok")], ["definitely-not-a-binary-xyz"],
        max_attempts=1,
    )
    assert rec.status == "failed"
    assert rec.attempts[0].verification.exit_code is None
    assert rec.attempts[0].verification.passed is False


def test_receipt_evidence_labels_and_no_task_text(repo):
    d = run_bounded_loop(repo, "secret task text", lambda c: [FileWriteAction("out.txt", "ok")], CHECK).to_dict()
    assert d["evidence"]["actions"] == "agent_declared"
    assert d["evidence"]["verification"] == "openshard_observed"
    assert "secret task text" not in str(d)
    assert d["attempts"][0]["verification"]["observed"] is True


def test_verifier_timeout_is_failure(repo):
    slow = [PY, "-c", "import time; print('x', flush=True); time.sleep(30)"]
    rec = run_bounded_loop(
        repo, "t", lambda c: [FileWriteAction("out.txt", "ok")], slow,
        max_attempts=1, verify_timeout=1.0,
    )
    v = rec.attempts[0].verification
    assert rec.status == "failed" and v.timed_out and v.exit_code is None and not v.passed


def test_sandbox_must_be_separate_from_repo(repo):
    with pytest.raises(ValueError):
        run_bounded_loop(repo, "t", lambda c: [FileWriteAction("out.txt", "ok")], CHECK, sandbox_path=repo)
    with pytest.raises(ValueError):
        run_bounded_loop(repo, "t", lambda c: [], CHECK, sandbox_path=repo / "sub")
    assert (repo / "out.txt").read_text() == "bad"


def test_receipt_object_does_not_retain_task_text(repo):
    rec = run_bounded_loop(repo, "secret task text", lambda c: [], CHECK)
    assert "secret task text" not in repr(rec)


def test_isolated_copy_excludes_local_secrets_and_agent_state(tmp_path):
    from openshard.osn.loop import create_isolated_copy

    r = tmp_path / "r"
    (r / ".claude").mkdir(parents=True)
    (r / ".claude" / "settings.json").write_text("{}")
    (r / ".env").write_text("K=v")
    (r / "keep.py").write_text("x")
    copy = create_isolated_copy(r)
    names = {p.name for p in copy.rglob("*")}
    assert "keep.py" in names and ".env" not in names and "settings.json" not in names


def test_policy_summary_masks_unsafe_paths_on_every_platform(repo):
    import json

    # "C:/x" is rejected as absolute on Windows but reaches the policy gate on
    # POSIX; either way the stored receipt must not carry the raw path.
    rec = run_bounded_loop(repo, "t", lambda c: [FileWriteAction("C:/Windows/evil.txt", "x")], CHECK)
    assert "Windows" not in json.dumps(rec.to_dict())
