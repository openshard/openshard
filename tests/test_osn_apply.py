"""`openshard osn apply <run-id>`: a verified result reaches the repository later, without re-running anything.

A verified run that was not promoted keeps exactly the bytes OpenShard
verified under its checkpoint. `osn apply` puts them in the repository
through the same policy gate as `--promote`, optionally commits them and
binds a re-verification to that commit. It refuses when the run did not
verify, was already applied or promoted, HEAD moved, or a target file
changed since the run finished. Interactive runs are offered the apply
once at the end; piped runs are told the command instead.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.osn import checkpoint as ckpt
from openshard.providers.base import BaseProvider, ChatResponse, UsageStats

PY = sys.executable
VERIFY = f'"{PY}" -c "import sys; sys.exit(0 if open(\'out.txt\').read()==\'ok\' else 1)"'
TASK = "make out ok"


class FakeProvider(BaseProvider):
    def __init__(self, replies):
        self.replies = list(replies)

    def list_models(self):
        return []

    def get_model_info(self, model_id):
        return None

    def execute(self, model, prompt, system=None, max_tokens=None):
        return ChatResponse(self.replies.pop(0), model, UsageStats(10, 5, 15, 0.001, cost_source="provider_reported"))


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "osn@example.com")
    _git(repo, "config", "user.name", "OSN Test")
    (repo / "out.txt").write_text("bad")
    (repo / "README.md").write_text("# demo\n")
    (repo / ".gitignore").write_text(".openshard/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def _turn(*actions):
    return json.dumps({"actions": list(actions), "note": "n"})


def _write(path, content):
    return {"kind": "write_file", "path": path, "content": content, "intent": "write"}


def _run(monkeypatch, repo, *extra, content="ok"):
    monkeypatch.chdir(repo)
    fake = FakeProvider([_turn(_write("out.txt", content), {"kind": "finish", "intent": "done"})])
    monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
    monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
    return CliRunner().invoke(cli, ["osn", "run", TASK, "--model", "fake/m", "--verify-cmd", VERIFY,
                                    "--roles", "executor", "--no-learning", *extra])


def _only_checkpoint(repo) -> ckpt.RunCheckpoint:
    runs = ckpt.list_checkpoints(repo)
    assert len(runs) == 1
    return runs[0]


def _last_entry(repo):
    return [json.loads(x) for x in (repo / ".openshard" / "runs.jsonl").read_text().splitlines()][-1]


class TestRetention:
    def test_a_verified_unpromoted_run_keeps_its_verified_bytes_and_says_how_to_apply(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        r = _run(monkeypatch, repo)
        assert r.exit_code == 0, r.output
        cp = _only_checkpoint(repo)
        assert cp.status == "completed" and cp.applied is None
        assert cp.verified_files == {"out.txt": cp.files["out.txt"]}
        assert cp.base_files == {"out.txt": ckpt.hash_repo_files(repo, ["out.txt"])["out.txt"]}
        kept = ckpt.checkpoint_dir(repo, cp.run_id) / ckpt.FILES_DIR / "out.txt"
        assert kept.read_text() == "ok" and (repo / "out.txt").read_text() == "bad"
        assert f"Apply later with: openshard osn apply {cp.run_id} [--commit]" in r.output
        assert "re-run with --promote" not in r.output
        listing = CliRunner().invoke(cli, ["osn", "runs"])
        assert f"verified, not applied: openshard osn apply {cp.run_id}" in listing.output

    def test_a_promoted_run_and_a_failed_run_retain_nothing(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        r = _run(monkeypatch, repo, "--promote", "--json")
        assert json.loads(r.output)["promoted"] == ["out.txt"]
        cp = _only_checkpoint(repo)
        assert cp.verified_files is None and cp.files == {} and cp.applied is None
        assert not (ckpt.checkpoint_dir(repo, cp.run_id) / ckpt.FILES_DIR).exists()
        refused = CliRunner().invoke(cli, ["osn", "apply", cp.run_id])
        assert refused.exit_code != 0 and ckpt.REFUSE_NO_VERIFIED_FILES in refused.output

        (tmp_path / "second").mkdir()
        failed_repo = _repo(tmp_path / "second")
        r2 = _run(monkeypatch, failed_repo, "--max-attempts", "1", content="still bad")
        assert r2.exit_code == 0, r2.output
        cp2 = _only_checkpoint(failed_repo)
        assert cp2.verified_files is None and "osn apply" not in r2.output


class TestApply:
    def test_apply_puts_the_verified_bytes_in_the_repository_once(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        r = CliRunner().invoke(cli, ["osn", "apply", cp.run_id])
        assert r.exit_code == 0, r.output
        assert "Applied 1 verified file(s)" in r.output and "applied: out.txt" in r.output
        assert "Not re-verified in the repository" in r.output
        assert (repo / "out.txt").read_text() == "ok"
        done = ckpt.read_checkpoint(repo, cp.run_id)
        assert done.applied["how"] == "osn_apply" and done.applied["files_applied"] == ["out.txt"]
        assert done.applied["commit"] is None and done.verified_files is None and done.files == {}
        assert not (ckpt.checkpoint_dir(repo, cp.run_id) / ckpt.FILES_DIR).exists()
        apply_log = (repo / ".openshard" / "sandbox_apply_receipts.jsonl").read_text()
        # The apply receipt points at the run the same way `--promote` does (its run id).
        assert _last_entry(repo)["timestamp"] in apply_log and '"files_applied": ["out.txt"]' in apply_log
        assert "applied to the repository" in CliRunner().invoke(cli, ["osn", "runs"]).output

        again = CliRunner().invoke(cli, ["osn", "apply", cp.run_id])
        assert again.exit_code != 0 and ckpt.REFUSE_ALREADY_APPLIED in again.output

    def test_apply_commit_binds_a_re_verification_to_the_new_commit(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        base = _git(repo, "rev-parse", "HEAD")
        r = CliRunner().invoke(cli, ["osn", "apply", cp.run_id, "--commit", "--json"])
        assert r.exit_code == 0, r.output
        body = json.loads(r.output)
        head = _git(repo, "rev-parse", "HEAD")
        assert head != base and body["commit"]["sha"] == head and body["applied"] == ["out.txt"]
        assert body["bound_verification"]["bound"] is True and body["bound_verification"]["status"] == "passed"
        assert body["bound_verification"]["artifact_sha"] == head
        message = _git(repo, "log", "-1", "--format=%B")
        assert f"Receipt: {cp.receipt_id}" in message
        attestations = (repo / ".openshard" / "verifications.jsonl").read_text()
        assert head in attestations and cp.receipt_id in attestations
        done = ckpt.read_checkpoint(repo, cp.run_id)
        assert done.applied["commit"]["sha"] == head and done.applied["bound_verification"]["bound"] is True

    def test_apply_refuses_when_a_target_file_changed_or_head_moved(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        (repo / "out.txt").write_text("edited by hand meanwhile")
        r = CliRunner().invoke(cli, ["osn", "apply", cp.run_id])
        assert r.exit_code != 0 and f"{ckpt.REFUSE_TARGET_CHANGED} (out.txt)" in r.output
        assert (repo / "out.txt").read_text() == "edited by hand meanwhile"
        assert "verified, not applied (repository changed since)" in CliRunner().invoke(cli, ["osn", "runs"]).output

        (repo / "out.txt").write_text("bad")  # back to what the run saw: applicable again
        assert ckpt.check_applicable(ckpt.read_checkpoint(repo, cp.run_id), repo).ok
        (repo / "README.md").write_text("# demo\nunrelated edit\n")  # unrelated dirt is fine
        assert ckpt.check_applicable(ckpt.read_checkpoint(repo, cp.run_id), repo).ok
        _git(repo, "commit", "-qam", "moved on")
        r2 = CliRunner().invoke(cli, ["osn", "apply", cp.run_id])
        assert r2.exit_code != 0 and ckpt.REFUSE_REPO_CHANGED in r2.output

    def test_apply_refuses_bytes_that_no_longer_match_the_verified_hashes(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        (ckpt.checkpoint_dir(repo, cp.run_id) / ckpt.FILES_DIR / "out.txt").write_text("tampered")
        r = CliRunner().invoke(cli, ["osn", "apply", cp.run_id])
        assert r.exit_code != 0 and ckpt.REFUSE_FILES_MISSING in r.output
        assert (repo / "out.txt").read_text() == "bad"

    def test_apply_refuses_unknown_and_incomplete_runs(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        monkeypatch.chdir(repo)
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        missing = CliRunner().invoke(cli, ["osn", "apply", "osn-nope"])
        assert missing.exit_code != 0 and ckpt.REFUSE_MISSING in missing.output
        cp = ckpt.RunCheckpoint(run_id="osn-abc", task=TASK, verify_argv=["x"], args={}, repo=ckpt.repo_fingerprint(repo),
                                models=["m"], status=ckpt.STATUS_INTERRUPTED, phase=ckpt.PHASE_ATTEMPT_DONE)
        ckpt.write_checkpoint(repo, cp)
        r = CliRunner().invoke(cli, ["osn", "apply", "osn-abc"])
        assert r.exit_code != 0 and ckpt.REFUSE_NOT_COMPLETED in r.output


class TestEndOfRunPrompt:
    def _tty(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.isatty", lambda: True, raising=False)
        monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)

    def test_an_interactive_yes_applies_through_the_policy_gate(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        monkeypatch.chdir(repo)
        fake = FakeProvider([_turn(_write("out.txt", "ok"), {"kind": "finish", "intent": "done"})])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        import openshard.cli.osn_cmd as osn_cmd

        asked: list[str] = []

        def confirm(text, default=False):
            asked.append(text)
            return True

        monkeypatch.setattr(osn_cmd, "_offer_apply", _interactive_offer(osn_cmd, confirm))
        r = CliRunner().invoke(cli, ["osn", "run", TASK, "--model", "fake/m", "--verify-cmd", VERIFY,
                                     "--roles", "executor", "--no-learning"])
        assert r.exit_code == 0, r.output
        assert asked == ["\nApply 1 verified file(s) to the repository now?"]
        assert (repo / "out.txt").read_text() == "ok"
        assert "Applied 1 file(s) into the repository (not re-verified there)." in r.output
        cp = _only_checkpoint(repo)
        assert cp.applied["how"] == "end_of_run_prompt" and cp.verified_files is None
        assert not (ckpt.checkpoint_dir(repo, cp.run_id) / ckpt.FILES_DIR).exists()

    def test_an_interactive_no_keeps_the_result_for_later(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        monkeypatch.chdir(repo)
        fake = FakeProvider([_turn(_write("out.txt", "ok"), {"kind": "finish", "intent": "done"})])
        monkeypatch.setattr("openshard.cli.osn_cmd._resolve_provider", lambda n, m: ("fake", fake))
        monkeypatch.setattr("openshard.cli.ingest._repo_root", lambda a, b: repo.resolve())
        import openshard.cli.osn_cmd as osn_cmd

        monkeypatch.setattr(osn_cmd, "_offer_apply", _interactive_offer(osn_cmd, lambda text, default=False: False))
        r = CliRunner().invoke(cli, ["osn", "run", TASK, "--model", "fake/m", "--verify-cmd", VERIFY,
                                     "--roles", "executor", "--no-learning"])
        assert r.exit_code == 0, r.output
        assert (repo / "out.txt").read_text() == "bad"
        cp = _only_checkpoint(repo)
        assert cp.applied is None and cp.verified_files == {"out.txt": cp.files["out.txt"]}
        assert f"Apply later with: openshard osn apply {cp.run_id}" in r.output


def _interactive_offer(osn_cmd, confirm):
    """The real `_offer_apply` with a terminal and a scripted answer (CliRunner has no TTY)."""
    real = osn_cmd._offer_apply

    def offer(repo_root, run_checkpoint, receipt, entry, permissions, assume_yes):
        import click

        saved_in, saved_out = sys.stdin, sys.stdout
        saved_confirm = click.confirm

        class _Tty:
            def __init__(self, inner):
                self._inner = inner

            def isatty(self):
                return True

            def __getattr__(self, name):
                return getattr(self._inner, name)

        sys.stdin, sys.stdout = _Tty(saved_in), _Tty(saved_out)
        click.confirm = confirm
        try:
            return real(repo_root, run_checkpoint, receipt, entry, permissions, assume_yes)
        finally:
            sys.stdin, sys.stdout = saved_in, saved_out
            click.confirm = saved_confirm

    return offer


def test_retain_verified_never_keeps_bytes_that_differ_from_the_verified_hashes(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    sandbox = tmp_path / "sb"
    sandbox.mkdir()
    (sandbox / "a.txt").write_text("verified")
    cp = ckpt.RunCheckpoint(run_id="osn-1", task="t", verify_argv=["x"], args={}, repo={}, models=["m"])
    import hashlib

    good = hashlib.sha256(b"verified").hexdigest()
    assert ckpt.retain_verified(repo, cp, sandbox, ["a.txt"], {"a.txt": good}) is True
    assert cp.verified_files == {"a.txt": good} and cp.base_files == {"a.txt": None}
    assert ckpt.retain_verified(repo, cp, sandbox, ["a.txt"], {"a.txt": "0" * 64}) is False
    assert cp.verified_files is None and cp.files == {}
    assert not (ckpt.checkpoint_dir(repo, "osn-1") / ckpt.FILES_DIR).exists()
    assert ckpt.retain_verified(repo, cp, sandbox, [], {}) is False


@pytest.mark.parametrize("field", ["verified_files", "base_files", "applied"])
def test_old_checkpoints_without_the_new_fields_still_load(tmp_path, field):
    repo = tmp_path / "r"
    repo.mkdir()
    cp = ckpt.RunCheckpoint(run_id="osn-old", task="t", verify_argv=["x"], args={}, repo={}, models=["m"])
    ckpt.write_checkpoint(repo, cp)
    path = ckpt.checkpoint_dir(repo, "osn-old") / ckpt.CHECKPOINT_FILE
    data = json.loads(path.read_text())
    del data[field]
    path.write_text(json.dumps(data))
    loaded = ckpt.read_checkpoint(repo, "osn-old")
    assert getattr(loaded, field) is None
    assert ckpt.check_applicable(loaded, repo).reason in (ckpt.REFUSE_NOT_COMPLETED,)
    assert isinstance(Path(path), Path)


class TestDiffAndListing:
    def test_diff_shows_what_the_verified_result_would_change(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        r = CliRunner().invoke(cli, ["osn", "diff", cp.run_id])
        assert r.exit_code == 0, r.output
        assert f"Verified result of run {cp.run_id} (Receipt {cp.receipt_id}) against the repository now · 1 file(s)" in r.output
        assert "--- a/out.txt" in r.output and "+++ b/out.txt" in r.output
        assert "-bad" in r.output and "+ok" in r.output
        assert (repo / "out.txt").read_text() == "bad"  # read-only

    def test_diff_names_why_a_result_is_not_applicable_and_refuses_applied_or_absent_results(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        (repo / "out.txt").write_text("edited meanwhile")
        r = CliRunner().invoke(cli, ["osn", "diff", cp.run_id])
        assert r.exit_code == 0, r.output
        assert f"not applicable: {ckpt.REFUSE_TARGET_CHANGED} (out.txt)" in r.output and "-edited meanwhile" in r.output
        (repo / "out.txt").write_text("bad")
        assert CliRunner().invoke(cli, ["osn", "apply", cp.run_id]).exit_code == 0
        applied = CliRunner().invoke(cli, ["osn", "diff", cp.run_id])
        assert applied.exit_code != 0 and "was applied at" in applied.output and "git diff" in applied.output
        missing = CliRunner().invoke(cli, ["osn", "diff", "osn-nope"])
        assert missing.exit_code != 0 and ckpt.REFUSE_MISSING in missing.output

        promoted_repo = tmp_path / "p"
        promoted_repo.mkdir()
        _run(monkeypatch, _repo(promoted_repo), "--promote", "--json")
        cp2 = _only_checkpoint(promoted_repo / "proj")
        none = CliRunner().invoke(cli, ["osn", "diff", cp2.run_id])
        assert none.exit_code != 0 and ckpt.REFUSE_NO_VERIFIED_FILES in none.output

    def test_a_completed_run_lists_its_real_attempt_count(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _run(monkeypatch, repo)
        cp = _only_checkpoint(repo)
        assert cp.result == {"status": "verified", "stop_reason": "verification_passed", "verification_state": "passed",
                             "attempts": 1, "changed_files": ["out.txt"]}
        listing = CliRunner().invoke(cli, ["osn", "runs", "--json"])
        row = json.loads(listing.output)[0]
        assert row["attempts_done"] == 1 and row["result"] == "verified_not_applied"
        assert "attempts 1" in CliRunner().invoke(cli, ["osn", "runs"]).output
