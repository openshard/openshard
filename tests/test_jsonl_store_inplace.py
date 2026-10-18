"""In-place upserts in the locked JSONL store (openshard.history.jsonl_store).

An upsert used to parse and rewrite the whole ``runs.jsonl`` for every
capture fold, so each hook event cost more as history grew. A replaced line
is now overwritten where it is when the record fits (a live Receipt is given
spare room), journaled so a crash is redone. These tests pin that reads are
unchanged, that line order and every other byte are preserved, that crashes
lose nothing, that concurrent writers stay correct, that seals still verify,
and that the per-update cost no longer grows with the history.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

from openshard.history import jsonl_store
from openshard.history import receipt_signing as rs
from openshard.history.jsonl_store import append_jsonl, upsert_jsonl, upsert_jsonl_with
from openshard.history.receipt_verify import verify_local
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.store import load_history


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _r(name: str) -> str:
    """A well-formed receipt id for a readable test name."""
    return "rcpt_" + hashlib.md5(name.encode()).hexdigest()


def _name(entry: dict) -> str:
    return entry.get("name", entry["receipt_id"])


def _live(name: str, *, ended: bool = False, pad: int = 0, **extra) -> dict:
    """A capture Receipt; still changing (so given spare room) until *ended*."""
    entry = {
        "timestamp": "2026-10-18T10:00:00Z", "task": "t", "receipt_id": _r(name), "name": name,
        "shard_id": f"shard_{name}",
        "capture": {"session_end_observed": ended, "last_activity_at": _now(),
                    **({"status": "ended"} if ended else {})},
        "events": [{"type": "tool.invoked", "n": i} for i in range(pad)],
        **extra,
    }
    entry["content_hash"] = compute_shard_hash(entry)
    return entry


def _by(name: str):
    return lambda e: e.get("receipt_id") == _r(name)


def _reference_upsert(lines: list[str], record: dict, match) -> list[str]:
    """The pre-change semantics: replace the first matching line, else append."""
    for i, raw in enumerate(lines):
        try:
            parsed = json.loads(raw)
        except ValueError:
            continue
        if isinstance(parsed, dict) and match(parsed):
            return lines[:i] + [json.dumps(record)] + lines[i + 1:]
    return lines + [json.dumps(record)]


def _ino(path: Path) -> int:
    return path.stat().st_ino


# ---------------------------------------------------------------------------
# Reads match the old behaviour; order and other bytes are kept
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("with_key", [True, False])
def test_reads_match_a_whole_file_rewrite_after_any_sequence(tmp_path: Path, with_key: bool) -> None:
    runs = tmp_path / "runs.jsonl"
    runs.write_text('{"receipt_id": "native", "task": "n"}\n\nnot json\n', encoding="utf-8")
    model = ['{"receipt_id": "native", "task": "n"}', "", "not json"]
    steps = [("a", 0), ("b", 0), ("a", 5), ("c", 0), ("b", 40), ("a", 400), ("c", 1), ("a", 2)]
    for turn, (rid, pad) in enumerate(steps):
        record = _live(rid, pad=pad, turn=turn)
        upsert_jsonl(runs, record, _by(rid), key=_r(rid) if with_key else None)
        model = _reference_upsert(model, record, _by(rid))
        assert load_history(runs, coerce=False) == [json.loads(m) for m in model if m and m != "not json"]
    # Last write wins, one line per receipt, the first writer's position kept.
    assert [_name(e) for e in load_history(runs, coerce=False)] == ["native", "a", "b", "c"]
    assert load_history(runs, coerce=False)[1]["turn"] == len(steps) - 1
    text = runs.read_text(encoding="utf-8")
    assert text.startswith('{"receipt_id": "native", "task": "n"}\n\nnot json\n')  # untouched bytes


def test_a_fitting_update_is_overwritten_in_place(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    append_jsonl(runs, {"receipt_id": "before"})
    upsert_jsonl(runs, _live("live"), _by("live"), key=_r("live"))
    append_jsonl(runs, {"receipt_id": "after"})
    ino, size = _ino(runs), runs.stat().st_size
    for turn in range(5):
        upsert_jsonl(runs, _live("live", pad=1, turn=turn), _by("live"), key=_r("live"))
    assert (_ino(runs), runs.stat().st_size) == (ino, size)  # no rewrite, nothing moved
    assert [_name(e) for e in load_history(runs, coerce=False)] == ["before", "live", "after"]
    assert load_history(runs, coerce=False)[1]["turn"] == 4
    assert not jsonl_store._redo_path(runs).exists()


def test_a_record_that_outgrew_its_line_is_rewritten_in_its_position(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    upsert_jsonl(runs, _live("live"), _by("live"), key=_r("live"))
    runs.write_bytes(runs.read_bytes() + b'{"receipt_id": "after"}\n')
    ino = _ino(runs)
    upsert_jsonl(runs, _live("live", pad=300), _by("live"), key=_r("live"))
    assert _ino(runs) != ino  # crash-safe temp+rename rewrite
    assert runs.read_bytes().endswith(b'{"receipt_id": "after"}\n')
    assert [_name(e) for e in load_history(runs, coerce=False)] == ["live", "after"]
    assert len(load_history(runs, coerce=False)[0]["events"]) == 300


def test_a_settled_record_gets_no_spare_room_and_other_files_none(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    done = _live("done", ended=True)
    upsert_jsonl(runs, done, _by("done"), key=_r("done"))
    assert runs.read_bytes() == (json.dumps(done) + os.linesep).encode()
    outbox = tmp_path / "outbox.jsonl"
    upsert_jsonl(outbox, _live("live"), _by("live"), key=_r("live"))
    assert not outbox.read_text(encoding="utf-8").rstrip("\r\n").endswith(" ")


def test_builder_sees_the_locked_lines_only_when_it_reads_them(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    append_jsonl(runs, {"receipt_id": "x"})
    seen: list[int] = []

    def build(lines, matched):
        seen.append(len(lines))
        return _live("live", n=len(lines))

    written, outcome = upsert_jsonl_with(runs, build, _by("live"), key=_r("live"))
    assert (outcome, written["n"], seen) == ("appended", 1, [1])
    given = []

    def update(lines, matched):
        given.append(lines)
        return {**matched, "turn": 2}

    written, outcome = upsert_jsonl_with(runs, update, _by("live"), key=_r("live"))
    assert outcome == "replaced" and given[0]._lines is None  # the history was never read
    assert load_history(runs, coerce=False)[1]["turn"] == 2


def test_an_index_left_stale_by_another_writer_is_not_trusted(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    upsert_jsonl(runs, _live("a"), _by("a"), key=_r("a"))
    upsert_jsonl(runs, _live("b"), _by("b"), key=_r("b"))
    # Another (older) Core rewrites the file with b first: offsets all change.
    a, b = (json.dumps(e) for e in load_history(runs, coerce=False))
    runs.write_text(b + "\n" + a + "\n", encoding="utf-8")
    upsert_jsonl(runs, _live("a", turn=9), _by("a"), key=_r("a"))
    stored = load_history(runs, coerce=False)
    assert [(_name(e), e.get("turn")) for e in stored] == [("b", None), ("a", 9)]


def test_a_record_that_stopped_changing_gives_back_its_spare_room(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    append_jsonl(runs, {"receipt_id": "before"})
    upsert_jsonl(runs, _live("s", pad=30), _by("s"), key=_r("s"))
    append_jsonl(runs, {"receipt_id": "after"})
    padded = runs.stat().st_size
    done = _live("s", ended=True, pad=30)
    upsert_jsonl(runs, done, _by("s"), key=_r("s"))
    lines = runs.read_bytes().split(os.linesep.encode())
    assert lines[1] == json.dumps(done).encode()  # compacted, no trailing spaces left
    assert runs.stat().st_size < padded
    assert [_name(e) for e in load_history(runs, coerce=False)] == ["before", "s", "after"]
    assert load_history(runs, coerce=False)[1] == done


def test_a_key_the_fast_search_misses_never_becomes_a_duplicate(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    key = "team/odd-receipt"
    runs.write_text('{"receipt_id": "team\\/odd-receipt", "turn": 0}\n', encoding="utf-8")  # "\/" is valid JSON
    by_key = lambda e: e.get("receipt_id") == key  # noqa: E731
    upsert_jsonl(runs, {"receipt_id": key, "turn": 1}, by_key, key=key)
    upsert_jsonl(runs, {"receipt_id": key, "turn": 2}, by_key, key=key)
    assert [e.get("turn") for e in load_history(runs, coerce=False)] == [2]


def test_index_survives_a_rewrite_that_moves_later_lines(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    for name in ("a", "b", "c"):
        upsert_jsonl(runs, _live(name), _by(name), key=_r(name))
    upsert_jsonl(runs, _live("a", pad=300), _by("a"), key=_r("a"))  # outgrows its line: b and c move
    ino = _ino(runs)
    upsert_jsonl(runs, _live("c", turn=5), _by("c"), key=_r("c"))
    assert _ino(runs) == ino  # found where it moved to and overwritten in place
    assert [(_name(e), e.get("turn")) for e in load_history(runs, coerce=False)] == [
        ("a", None), ("b", None), ("c", 5)]


def test_the_line_index_stays_bounded(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    for i in range(jsonl_store._MAX_INDEXED_KEYS + 20):
        upsert_jsonl(runs, {"receipt_id": f"k{i}"}, lambda e, i=i: e.get("receipt_id") == f"k{i}", key=f"k{i}")
    assert len(jsonl_store._line_index[str(runs)][1]) == jsonl_store._MAX_INDEXED_KEYS


# ---------------------------------------------------------------------------
# Crash safety
# ---------------------------------------------------------------------------


def _interrupt_overwrite(monkeypatch, *, torn: bool) -> None:
    """Make the next in-place overwrite stop after its journal (and half its bytes)."""
    real = jsonl_store._write_durably

    def crash(fh, data):
        if "+" in fh.mode:
            if torn:
                fh.write(data[: len(data) // 2])
                fh.flush()
            raise KeyboardInterrupt("simulated crash")
        real(fh, data)

    monkeypatch.setattr(jsonl_store, "_write_durably", crash)


@pytest.mark.parametrize("torn", [True, False])
def test_an_interrupted_overwrite_is_redone_by_the_next_locked_access(tmp_path, monkeypatch, torn) -> None:
    runs = tmp_path / "runs.jsonl"
    upsert_jsonl(runs, _live("a"), _by("a"), key=_r("a"))
    append_jsonl(runs, {"receipt_id": "later"})
    _interrupt_overwrite(monkeypatch, torn=torn)
    with pytest.raises(KeyboardInterrupt):
        upsert_jsonl(runs, _live("a", turn=7), _by("a"), key=_r("a"))
    monkeypatch.undo()
    assert jsonl_store._redo_path(runs).exists()
    with jsonl_store.history_file_lock(runs):  # any locked access, a reader's included
        pass
    assert not jsonl_store._redo_path(runs).exists()
    stored = load_history(runs, coerce=False)
    assert [(_name(e), e.get("turn")) for e in stored] == [("a", 7), ("later", None)]


def test_a_partial_or_stale_journal_is_dropped_without_touching_the_file(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    upsert_jsonl(runs, _live("a"), _by("a"), key=_r("a"))
    before = runs.read_bytes()
    redo = jsonl_store._redo_path(runs)
    redo.write_bytes(b'{"offset": 0, "length": 9')  # crash while writing the journal
    append_jsonl(runs, {"receipt_id": "b"})
    assert not redo.exists() and runs.read_bytes().startswith(before)
    st = runs.stat()
    payload = b"x" * 10 + b"\n"
    redo.write_bytes(json.dumps({"offset": 0, "length": len(payload), "dev": st.st_dev, "ino": st.st_ino + 1,
                                 "sha256": hashlib.sha256(payload).hexdigest()}).encode() + b"\n" + payload)
    current = runs.read_bytes()
    append_jsonl(runs, {"receipt_id": "c"})  # the journal names another file: dropped
    assert not redo.exists() and runs.read_bytes().startswith(current)


# ---------------------------------------------------------------------------
# Concurrent writers
# ---------------------------------------------------------------------------


def _upsert_worker(args: tuple[str, int, int]) -> None:
    path, proc, count = args
    for i in range(count):
        upsert_jsonl(Path(path), _live(f"p{proc}", turn=i, pad=i % 7), _by(f"p{proc}"), key=_r(f"p{proc}"))
        upsert_jsonl(Path(path), _live("shared", turn=i, by=proc), _by("shared"), key=_r("shared"))
        if i % 5 == 0:
            append_jsonl(Path(path), {"receipt_id": f"native-{proc}-{i}"})


def test_cross_process_upserts_keep_one_line_per_receipt(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    with mp.get_context("spawn").Pool(processes=4) as pool:
        pool.map(_upsert_worker, [(str(runs), proc, 20) for proc in range(4)])
    lines = [ln for ln in runs.read_text(encoding="utf-8").splitlines() if ln.strip()]
    stored = [json.loads(ln) for ln in lines]  # every line well-formed
    ids = [_name(e) for e in stored]
    assert len(ids) == len(set(ids)) == 4 + 1 + 4 * 4
    assert all(e["turn"] == 19 for e in stored if _name(e).startswith("p"))
    assert next(e for e in stored if _name(e) == "shared")["turn"] == 19


def test_threaded_upserts_keep_one_line_per_receipt(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    threads = [threading.Thread(target=_upsert_worker, args=((str(runs), proc, 15),)) for proc in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [_name(e) for e in load_history(runs, coerce=False)]
    assert len(ids) == len(set(ids)) == 4 + 1 + 4 * 3


# ---------------------------------------------------------------------------
# Seals
# ---------------------------------------------------------------------------


def test_seals_still_verify_after_in_place_updates_of_other_receipts(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(rs.DISABLE_ENV, raising=False)
    history = tmp_path / ".openshard"
    runs = history / "runs.jsonl"
    upsert_jsonl(runs, _live("live"), _by("live"), key=_r("live"))
    upsert_jsonl(runs, _live("sealed"), _by("sealed"), key=_r("sealed"))
    upsert_jsonl(runs, _live("sealed", ended=True), _by("sealed"), key=_r("sealed"))
    assert len([ln for ln in rs.read_sidecar(history) if rs.is_envelope(ln)]) == 1
    for turn in range(3):
        upsert_jsonl(runs, _live("live", turn=turn), _by("live"), key=_r("live"))
    stored = load_history(runs, coerce=False)
    assert verify_local(runs, stored[1], 1).verdict == rs.VERDICT_VERIFIED
    upsert_jsonl(runs, _live("live", ended=True), _by("live"), key=_r("live"))
    stored = load_history(runs, coerce=False)
    assert [verify_local(runs, e, i).verdict for i, e in enumerate(stored)] == [rs.VERDICT_VERIFIED] * 2


# ---------------------------------------------------------------------------
# Cost no longer grows with the history
# ---------------------------------------------------------------------------


def _best_update_seconds(runs: Path, rounds: int) -> float:
    best = float("inf")
    for turn in range(rounds):
        start = time.perf_counter()
        upsert_jsonl(runs, _live("live", pad=20, turn=turn), _by("live"), key=_r("live"))
        best = min(best, time.perf_counter() - start)
    return best


def test_live_receipt_update_cost_stays_flat_as_history_grows(tmp_path: Path) -> None:
    """A ratio of best-of-N timings, not absolute ms, so parallel load does not flake it.

    Before the change the large history cost ~30x the small one (a parse and
    rewrite of every byte); now an update touches only its own line.
    """
    filler = json.dumps({"receipt_id": "old", "events": [{"n": i, "pad": "x" * 40} for i in range(300)]})
    costs = []
    for name, records in (("small", 20), ("large", 800)):  # ~0.5 MB and ~20 MB
        runs = tmp_path / name / "runs.jsonl"
        runs.parent.mkdir()
        runs.write_text("".join(filler.replace('"old"', f'"old{i}"') + "\n" for i in range(records)))
        upsert_jsonl(runs, _live("live", pad=20), _by("live"), key=_r("live"))
        runs.write_bytes(runs.read_bytes() + (filler + "\n").encode())  # the live line is not last
        upsert_jsonl(runs, _live("live", pad=20), _by("live"), key=_r("live"))  # warm the index
        ino = _ino(runs)
        costs.append(_best_update_seconds(runs, 15))
        assert _ino(runs) == ino  # every update was in place
    small, large = costs
    assert large < max(small * 4, small + 0.01), f"small {small * 1e3:.2f} ms, large {large * 1e3:.2f} ms"
