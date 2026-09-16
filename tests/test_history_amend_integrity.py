"""Regression tests: legitimate OpenShard amendments keep ``content_hash`` truthful.

Before this fix ``openshard note`` and ``openshard feedback`` rewrote the
latest record with new metadata but kept the old ``content_hash``, so a fresh
Receipt went from ``Integrity: Matches`` to ``Integrity: Mismatch`` after
OpenShard itself added a note. These tests pin the canonical loader, the
canonical latest-record lookup and the canonical amendment path in
``openshard.history.store``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from openshard.cli.main import cli
from openshard.history import store
from openshard.history.jsonl_store import append_jsonl
from openshard.history.receipt_identity import ensure_receipt_id
from openshard.history.shard_contract import build_shard_receipt
from openshard.history.shard_hash import compute_shard_hash, verify_shard_hash
from openshard.history.shard_schema import coerce_shard_entry

RUNS_REL = Path(".openshard") / "runs.jsonl"


def _fresh_entry(**overrides) -> dict:
    """A record exactly as a v0.4.4 writer persists it: receipt id + coerced + hashed."""
    entry = {
        "schema_version": "1.2",
        "timestamp": "2026-09-16T10:00:00",
        "task": "do X",
        "summary": "done",
        "verification_passed": True,
    }
    entry.update(overrides)
    ensure_receipt_id(entry)
    return coerce_shard_entry(entry)


def _legacy_entry() -> dict:
    """A pre-hash record: no schema_version, no content_hash, no receipt_id."""
    return {"timestamp": "2026-01-01T00:00:00", "task": "old task", "summary": "old"}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(repo: Path, *entries: dict) -> Path:
    runs = repo / RUNS_REL
    for entry in entries:
        append_jsonl(runs, entry)
    return runs


def _raw_last(runs: Path) -> dict:
    return json.loads(runs.read_text(encoding="utf-8").splitlines()[-1])


def _last_json(runner: CliRunner) -> dict:
    result = runner.invoke(cli, ["last", "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _last_human(runner: CliRunner) -> str:
    result = runner.invoke(cli, ["last"])
    assert result.exit_code == 0, result.output
    return result.output


# ---------------------------------------------------------------------------
# The bug: fresh -> Matches, note -> still Matches, feedback -> still Matches
# ---------------------------------------------------------------------------


class TestAmendmentKeepsIntegrity:
    def test_fresh_receipt_matches(self, repo: Path):
        _write(repo, _fresh_entry())
        runner = CliRunner()
        assert _last_json(runner)["content_hash_status"] == "valid"
        assert "Matches (content hash)" in _last_human(runner)

    def test_note_keeps_matches(self, repo: Path):
        runs = _write(repo, _fresh_entry())
        runner = CliRunner()
        result = runner.invoke(cli, ["note", "looks right"])
        assert result.exit_code == 0, result.output

        stored = _raw_last(runs)
        assert stored["notes"][0]["text"] == "looks right"
        assert verify_shard_hash(stored)["status"] == "valid"
        assert _last_json(runner)["content_hash_status"] == "valid"
        assert "Matches (content hash)" in _last_human(runner)
        assert "Mismatch" not in _last_human(runner)

    def test_feedback_keeps_matches(self, repo: Path):
        runs = _write(repo, _fresh_entry())
        runner = CliRunner()
        result = runner.invoke(cli, ["feedback", "accept", "--edited"])
        assert result.exit_code == 0, result.output

        stored = _raw_last(runs)
        assert stored["developer_feedback"]["outcome"] == "accepted"
        assert verify_shard_hash(stored)["status"] == "valid"
        assert _last_json(runner)["content_hash_status"] == "valid"
        assert "Matches (content hash)" in _last_human(runner)

    def test_feedback_note_subcommand_keeps_matches(self, repo: Path):
        runs = _write(repo, _fresh_entry())
        runner = CliRunner()
        assert runner.invoke(cli, ["feedback", "note", "free text"]).exit_code == 0
        assert verify_shard_hash(_raw_last(runs))["status"] == "valid"

    def test_note_then_feedback_then_note_all_match(self, repo: Path):
        runs = _write(repo, _fresh_entry())
        runner = CliRunner()
        for args in (["note", "one"], ["feedback", "reject", "--reason", "nope"], ["note", "two"]):
            assert runner.invoke(cli, args).exit_code == 0
            assert verify_shard_hash(_raw_last(runs))["status"] == "valid"
        stored = _raw_last(runs)
        assert [n["text"] for n in stored["notes"]] == ["one", "two"]
        assert stored["developer_feedback"]["outcome"] == "rejected"

    def test_hash_actually_changes_to_cover_the_new_content(self, repo: Path):
        entry = _fresh_entry()
        runs = _write(repo, entry)
        CliRunner().invoke(cli, ["note", "hello"])
        stored = _raw_last(runs)
        assert stored["content_hash"] != entry["content_hash"]
        assert stored["content_hash"] == compute_shard_hash(stored)

    def test_receipt_id_and_shard_id_unchanged_by_amendments(self, repo: Path):
        entry = _fresh_entry()
        _write(repo, entry)
        runner = CliRunner()
        before = _last_json(runner)
        runner.invoke(cli, ["note", "hello"])
        runner.invoke(cli, ["feedback", "accept"])
        after = _last_json(runner)
        assert before["receipt_id"] == entry["receipt_id"] == after["receipt_id"]
        assert before["shard_id"] == after["shard_id"]

    def test_amendment_metadata_is_additive_and_truthful(self, repo: Path):
        runs = _write(repo, _fresh_entry())
        runner = CliRunner()
        runner.invoke(cli, ["note", "hello"])
        runner.invoke(cli, ["feedback", "accept"])
        amendments = _raw_last(runs)["amendments"]
        assert [a["kind"] for a in amendments] == ["note", "developer_feedback"]
        for a in amendments:
            assert a["schema_version"] == 1
            assert a["source"] == "cli"
            assert a["integrity_before"] == "valid"
            assert a["content_hash_restamped"] is True
            assert a["recorded_at"]

    def test_only_the_latest_record_is_touched(self, repo: Path):
        first = _fresh_entry(task="first")
        runs = _write(repo, first, _fresh_entry(task="second"))
        raw_first_line = runs.read_text(encoding="utf-8").splitlines()[0]
        CliRunner().invoke(cli, ["note", "hello"])
        lines = runs.read_text(encoding="utf-8").splitlines()
        assert lines[0] == raw_first_line
        assert "notes" in json.loads(lines[1])
        assert verify_shard_hash(json.loads(lines[0]))["status"] == "valid"


# ---------------------------------------------------------------------------
# Old history stays readable; historical mismatches are not rewritten
# ---------------------------------------------------------------------------


class TestOldHistoryCompatibility:
    def test_legacy_record_is_readable_and_not_recorded(self, repo: Path):
        _write(repo, _legacy_entry())
        runner = CliRunner()
        payload = _last_json(runner)
        assert payload["status"] == "ok"
        assert payload["content_hash_status"] == "missing"
        assert payload["content_hash"] is None
        assert "Not recorded" in _last_human(runner)

    def test_loader_never_fabricates_a_hash_on_read(self, repo: Path):
        runs = _write(repo, _legacy_entry())
        records = store.load_history(runs)
        assert len(records) == 1
        assert "content_hash" not in records[0]
        assert records[0]["schema_version"] == "unknown"
        # The file itself is untouched by reading.
        assert "content_hash" not in _raw_last(runs)

    def test_note_on_legacy_record_stays_not_recorded(self, repo: Path):
        runs = _write(repo, _legacy_entry())
        runner = CliRunner()
        assert runner.invoke(cli, ["note", "late note"]).exit_code == 0
        stored = _raw_last(runs)
        assert stored["notes"][0]["text"] == "late note"
        assert "content_hash" not in stored
        assert stored["amendments"][0]["integrity_before"] == "missing"
        assert stored["amendments"][0]["content_hash_restamped"] is False
        assert _last_json(runner)["content_hash_status"] == "missing"
        # Legacy fields all survive the round trip.
        assert stored["task"] == "old task" and stored["summary"] == "old"
        assert "schema_version" not in stored  # historical content not re-coerced on disk

    def test_mismatched_record_is_not_silently_reblessed(self, repo: Path):
        tampered = _fresh_entry()
        tampered["summary"] = "edited after the hash was written"
        stale_hash = tampered["content_hash"]
        runs = _write(repo, tampered)
        runner = CliRunner()
        assert _last_json(runner)["content_hash_status"] == "mismatch"

        assert runner.invoke(cli, ["note", "still mismatched"]).exit_code == 0
        stored = _raw_last(runs)
        assert stored["content_hash"] == stale_hash
        assert stored["notes"][0]["text"] == "still mismatched"
        assert stored["amendments"][0]["integrity_before"] == "mismatch"
        assert stored["amendments"][0]["content_hash_restamped"] is False
        assert _last_json(runner)["content_hash_status"] == "mismatch"
        assert "Mismatch (content hash)" in _last_human(runner)

    def test_mixed_history_each_record_keeps_its_own_verdict(self, repo: Path):
        runs = _write(repo, _legacy_entry(), _fresh_entry())
        records = store.load_history(runs)
        assert verify_shard_hash(records[0])["status"] == "missing"
        assert verify_shard_hash(records[1])["status"] == "valid"
        assert build_shard_receipt(records[0], index=0).integrity == "Not recorded"
        assert build_shard_receipt(records[1], index=1).integrity == "Matches (content hash)"


# ---------------------------------------------------------------------------
# Subdirectories resolve the same history
# ---------------------------------------------------------------------------


class TestSubdirectoryResolution:
    def test_note_from_subdirectory_amends_root_history(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        runs = _write(repo, _fresh_entry())
        deep = repo / "src" / "pkg" / "deep"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)
        runner = CliRunner()
        assert runner.invoke(cli, ["note", "from below"]).exit_code == 0
        assert runner.invoke(cli, ["feedback", "accept"]).exit_code == 0

        stored = _raw_last(runs)
        assert stored["notes"][0]["text"] == "from below"
        assert stored["developer_feedback"]["outcome"] == "accepted"
        assert verify_shard_hash(stored)["status"] == "valid"
        # Feedback side-records land next to the history that was amended.
        assert not (deep / ".openshard").exists()
        assert not (repo / "src" / ".openshard").exists()
        assert (repo / ".openshard" / "interactions.jsonl").exists()
        assert (repo / ".openshard" / "memory.jsonl").exists()

    def test_last_from_subdirectory_sees_the_amendment(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _write(repo, _fresh_entry())
        deep = repo / "src" / "deep"
        deep.mkdir(parents=True)
        runner = CliRunner()
        runner.invoke(cli, ["note", "root note"])  # from the root
        monkeypatch.chdir(deep)
        payload = _last_json(runner)
        assert payload["repo"]["from_subdirectory"] is True
        assert payload["content_hash_status"] == "valid"
        assert "root note" in runner.invoke(cli, ["last", "--more"]).output

    def test_no_history_from_subdirectory_fails_cleanly(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ):
        deep = repo / "src"
        deep.mkdir()
        monkeypatch.chdir(deep)
        runner = CliRunner()
        note = runner.invoke(cli, ["note", "x"])
        assert note.exit_code != 0 and "No run history found" in note.output
        fb = runner.invoke(cli, ["feedback", "accept"])
        assert fb.exit_code != 0 and "No run history found" in fb.output
        assert not (repo / RUNS_REL).exists()


# ---------------------------------------------------------------------------
# Malformed records fail safely
# ---------------------------------------------------------------------------


class TestMalformedRecords:
    def test_malformed_lines_are_skipped_on_read_and_preserved_on_write(self, repo: Path):
        runs = repo / RUNS_REL
        runs.parent.mkdir(parents=True)
        good = _fresh_entry()
        runs.write_text(
            "{not json\n"
            "\n"
            + json.dumps(good) + "\n"
            + '"a bare string"\n'
            + "[1, 2, 3]\n"
            + '{"truncated": \n',
            encoding="utf-8",
        )
        records = store.load_history(runs)
        assert len(records) == 1 and records[0]["task"] == "do X"
        assert store.latest_record(runs) == (0, records[0])

        runner = CliRunner()
        assert runner.invoke(cli, ["note", "hello"]).exit_code == 0
        lines = runs.read_text(encoding="utf-8").splitlines()
        assert lines[0] == "{not json"
        assert lines[1] == ""
        assert lines[3] == '"a bare string"'
        assert lines[4] == "[1, 2, 3]"
        assert lines[5] == '{"truncated": '
        amended = json.loads(lines[2])
        assert amended["notes"][0]["text"] == "hello"
        assert verify_shard_hash(amended)["status"] == "valid"
        assert _last_json(runner)["content_hash_status"] == "valid"

    def test_only_malformed_lines_means_no_history(self, repo: Path):
        runs = repo / RUNS_REL
        runs.parent.mkdir(parents=True)
        runs.write_text("garbage\n[1]\n", encoding="utf-8")
        assert store.load_history(runs) == []
        assert store.latest_record(runs) is None
        assert store.amend_latest_record(runs, "note", lambda r: None) is None
        assert runs.read_text(encoding="utf-8") == "garbage\n[1]\n"
        result = CliRunner().invoke(cli, ["note", "x"])
        assert result.exit_code != 0 and "No run history found" in result.output

    def test_missing_and_empty_files(self, repo: Path):
        runs = repo / RUNS_REL
        assert store.load_history(runs) == []
        assert store.latest_record(runs) is None
        assert store.amend_latest_record(runs, "note", lambda r: None) is None
        assert not runs.exists()
        assert not runs.parent.exists()  # no side effect when nothing was amended
        runs.parent.mkdir(parents=True)
        runs.write_text("", encoding="utf-8")
        assert store.load_history(runs) == []
        assert store.amend_latest_record(runs, "note", lambda r: None) is None
        assert runs.read_text(encoding="utf-8") == ""

    def test_blocked_fields_stripped_on_read_but_never_written_back(self, repo: Path):
        record = _legacy_entry()
        record["raw_prompt"] = "secret prompt"
        runs = _write(repo, record)
        assert "raw_prompt" not in store.load_history(runs)[0]
        CliRunner().invoke(cli, ["note", "x"])
        # Amendment never re-shapes historical content on disk either way.
        stored = _raw_last(runs)
        assert stored["raw_prompt"] == "secret prompt"
        assert stored["notes"][0]["text"] == "x"

    def test_corrupt_existing_notes_or_amendments_are_replaced_not_crashed(self, repo: Path):
        runs = _write(repo, _fresh_entry(notes="not a list", amendments={"bad": True}))
        runner = CliRunner()
        assert runner.invoke(cli, ["note", "fresh"]).exit_code == 0
        stored = _raw_last(runs)
        assert stored["notes"] == [stored["notes"][0]]
        assert stored["notes"][0]["text"] == "fresh"
        assert isinstance(stored["amendments"], list) and len(stored["amendments"]) == 1
        assert verify_shard_hash(stored)["status"] == "valid"
