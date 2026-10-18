"""History reads that do not parse all of ``runs.jsonl``.

Finding a session's record when a hook arrives without a buffer, finding the
record a seal covers, minting a new Receipt's Shard id and counting history
lines each used to parse (or decode) the whole history, so the per-event cost
of capture grew with history size even after in-place upserts. These tests
pin that every answer is unchanged against the whole-file reading it
replaced, that Shard ids stay unique across processes, and that the lookups
no longer cost more as the history grows.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

from openshard.adapters import claude_hooks as ch
from openshard.history import jsonl_store
from openshard.history import receipt_signing as rs
from openshard.history.jsonl_store import (
    count_lines,
    find_last_record,
    holds_value,
    line_count,
    upsert_jsonl,
    upsert_jsonl_with,
)
from openshard.history.receipt_verify import verify_local
from openshard.history.shard_contract import _make_shard_id
from openshard.history.shard_hash import compute_shard_hash
from openshard.history.store import load_history

TS = "2026-10-18T10:00:00Z"


def _r(name: str) -> str:
    return "rcpt_" + hashlib.md5(name.encode()).hexdigest()


def _session(name: str, sid: str, *, ended: bool = False, executor: str = ch.EXECUTOR, pad: int = 0, **extra) -> dict:
    entry = {
        "timestamp": TS, "task": "t", "executor": executor, "receipt_id": _r(name), "name": name,
        "shard_id": f"shard_{name}",
        "capture": {"session_id": sid, "session_end_observed": ended, **({"status": "ended"} if ended else {})},
        "events": [{"n": i, "pad": "x" * 40} for i in range(pad)],
        **extra,
    }
    entry["content_hash"] = compute_shard_hash(entry)
    return entry


def _old_find_persisted_entry(repo_root: Path, session_id: str, executor: str = ch.EXECUTOR) -> dict | None:
    """The whole-file scan ``claude_hooks._find_persisted_entry`` used before."""
    path = repo_root / ".openshard" / "runs.jsonl"
    if not path.exists():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and ch._is_session_entry(d, session_id, executor):
            found = d
    return found


def _messy_history(runs: Path, eol: str = "\n") -> None:
    """Sessions with several segments, a resumed record naming an earlier receipt,
    another executor with the same session id, blank and malformed lines, and a
    last line without its newline."""
    lines = [
        json.dumps(_session("a1", "sess-a", ended=True)),
        "",
        "{not json",
        json.dumps(_session("b1", "sess-b", pad=30)),
        json.dumps(_session("a2", "sess-a", resumed_from_receipt_id=_r("a1"))),
        json.dumps(_session("x1", "sess-a", executor="someone-else")),
        "[1, 2]",
        json.dumps(_session("odd", "sess/odd")),
        json.dumps(_session("c1", "sess-c", pad=5)),
    ]
    runs.parent.mkdir(parents=True, exist_ok=True)
    runs.write_bytes((eol.join(lines)).encode("utf-8"))


# ---------------------------------------------------------------------------
# Finding a record from the end of the file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
@pytest.mark.parametrize("block", [16, 64, 1 << 20])  # lines far longer than a block, and one block
def test_find_persisted_entry_matches_the_whole_file_scan(tmp_path, monkeypatch, eol, block) -> None:
    monkeypatch.setattr(jsonl_store, "_SCAN_BLOCK_BYTES", block)
    _messy_history(tmp_path / ".openshard" / "runs.jsonl", eol)
    for sid in ("sess-a", "sess-b", "sess-c", "sess/odd", "missing", ""):
        for executor in (ch.EXECUTOR, "someone-else"):
            assert ch._find_persisted_entry(tmp_path, sid, executor) == _old_find_persisted_entry(tmp_path, sid, executor)
    assert ch._find_persisted_entry(tmp_path, "sess-a")["name"] == "a2"  # the latest segment


def test_find_last_record_takes_no_lock_and_creates_nothing_for_a_missing_file(tmp_path: Path) -> None:
    assert find_last_record(tmp_path / ".openshard" / "runs.jsonl", lambda r: True, key="k") is None
    assert not (tmp_path / ".openshard").exists()
    assert ch._find_persisted_entry(tmp_path, "sess-a") is None


@pytest.mark.parametrize("block", [16, 1 << 20])
def test_find_last_record_without_a_plain_key_parses_from_the_end(tmp_path, monkeypatch, block) -> None:
    monkeypatch.setattr(jsonl_store, "_SCAN_BLOCK_BYTES", block)
    runs = tmp_path / "runs.jsonl"
    _messy_history(runs)
    reference = [r for r in load_history(runs, coerce=False) if r.get("executor") == ch.EXECUTOR]
    hit = find_last_record(runs, lambda r: r.get("executor") == ch.EXECUTOR)
    assert hit is not None and hit[1] == reference[-1]
    assert runs.read_bytes()[hit[0]:].startswith(json.dumps(reference[-1]).encode())


def test_seal_lookup_matches_load_history_and_the_projection_is_unchanged(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    _messy_history(runs)
    stored = load_history(runs, coerce=False)
    for index, entry in enumerate(stored):
        rid = entry["receipt_id"]
        found = rs._find_stored(runs, rid)
        assert found is not None and found[1] == rs._find(stored, rid)[1] == entry
        # A record with a shard_id never reads its index, so None projects the same.
        assert found[0] is None
        assert rs.receipt_projection(entry, None, []) == rs.receipt_projection(entry, index, [])
    assert rs._find_stored(runs, _r("missing")) is None


def test_a_record_without_a_shard_id_still_gets_its_history_index(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    _messy_history(runs)
    bare = {"timestamp": TS, "receipt_id": _r("bare"), "task": "t"}
    upsert_jsonl(runs, bare, lambda r: r.get("receipt_id") == _r("bare"), key=_r("bare"))
    stored = load_history(runs, coerce=False)
    assert rs._find_stored(runs, _r("bare")) == rs._find(stored, _r("bare")) == (len(stored) - 1, bare)


def test_sealing_after_many_receipts_still_verifies(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(rs.DISABLE_ENV, raising=False)
    history = tmp_path / ".openshard"
    runs = history / "runs.jsonl"
    for i in range(6):
        upsert_jsonl(runs, _session(f"s{i}", f"sess-{i}", pad=10), lambda r, i=i: r.get("name") == f"s{i}", key=_r(f"s{i}"))
    for i in (4, 1, 5):
        upsert_jsonl(runs, _session(f"s{i}", f"sess-{i}", ended=True, pad=10), lambda r, i=i: r.get("name") == f"s{i}",
                     key=_r(f"s{i}"))
    stored = load_history(runs, coerce=False)
    sidecar = rs.read_sidecar(history)
    sealed = {e["receipt_id"] for e in stored if rs.seals_for(sidecar, e["receipt_id"])}
    assert {_r("s4"), _r("s1"), _r("s5")} <= sealed
    for index, entry in enumerate(stored):
        if entry["receipt_id"] in sealed:
            assert verify_local(runs, entry, index).verdict == rs.VERDICT_VERIFIED


# ---------------------------------------------------------------------------
# Line counts and Shard ids by byte scan
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blob", [b"", b"a", b"a\n", b"a\nb", b"a\r\nb\r\n", b"a\rb\rc", b"\n\n\r\n", b"a\r", "é\n".encode()])
def test_count_lines_matches_a_text_mode_read(tmp_path: Path, blob: bytes) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_bytes(blob)
    with path.open(encoding="utf-8") as fh:
        assert count_lines(path) == sum(1 for _ in fh)
    assert count_lines(tmp_path / "missing.jsonl") == 0


def _locked_lines(path: Path) -> jsonl_store._LinesUnderLock:
    return jsonl_store._LinesUnderLock(path)


def test_line_count_and_holds_value_match_the_decoded_lines(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    _messy_history(runs, "\r\n")
    with runs.open(encoding="utf-8") as fh:
        decoded = fh.read().splitlines(keepends=True)
    assert line_count(_locked_lines(runs)) == line_count(decoded) == len(decoded)
    for value in ("shard_a1", "shard_c1", "shard_missing", "sess-a", "shard_odd"):
        assert holds_value(_locked_lines(runs), "shard_id", value) == holds_value(decoded, "shard_id", value)
    assert holds_value(_locked_lines(runs), "shard_id", "shard_b1")
    assert not holds_value(_locked_lines(runs), "shard_id", "sess-b")  # the value under another field


def _new_receipt(name: str) -> dict:
    return {"timestamp": TS, "receipt_id": _r(name), "shard_id": "provisional", "events": [{"shard_id": "provisional"}]}


def test_a_new_receipt_gets_the_same_shard_id_as_the_whole_file_reading(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    taken = [json.dumps({"receipt_id": _r(f"t{i}"), "shard_id": _make_shard_id(TS, i)}) for i in (4, 5, 7)]
    runs.write_text("\n".join([*taken, "", "{bad", json.dumps({"note": _make_shard_id(TS, 6)})]) + "\n")
    with runs.open(encoding="utf-8") as fh:
        decoded = fh.read().splitlines(keepends=True)
    fast = ch._finalize_hook_shard_id(_new_receipt("n"), {}, _locked_lines(runs), None)
    slow = ch._finalize_hook_shard_id(_new_receipt("n"), {}, decoded, None)
    assert fast == slow
    # 6 lines: index 6 is free (only a non-shard_id field names it); 4, 5 and 7 are held.
    assert fast["shard_id"] == _make_shard_id(TS, 6) and fast["events"][0]["shard_id"] == fast["shard_id"]


def _mint(runs: str, worker: int, count: int) -> None:
    path = Path(runs)
    for n in range(count):
        name = f"w{worker}-{n}"
        upsert_jsonl_with(
            path,
            lambda lines, matched, name=name: ch._finalize_hook_shard_id(_new_receipt(name), {}, lines, matched),
            lambda r, name=name: r.get("receipt_id") == _r(name),
            key=_r(name),
        )
        if n % 3 == 0:  # an in-place update of an earlier record between appends
            upsert_jsonl(path, _session(f"w{worker}", f"sess-w{worker}", turn=n),
                         lambda r, worker=worker: r.get("name") == f"w{worker}", key=_r(f"w{worker}"))


def test_shard_ids_stay_unique_across_processes(tmp_path: Path) -> None:
    runs = tmp_path / "runs.jsonl"
    # Ids ahead of their position, as a pruned or older history can hold.
    runs.write_text("".join(json.dumps({"receipt_id": _r(f"old{i}"), "shard_id": _make_shard_id(TS, i)}) + "\n"
                            for i in (3, 9, 10, 20)))
    ctx = mp.get_context("spawn")
    procs = [ctx.Process(target=_mint, args=(str(runs), w, 12)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    stored = load_history(runs, coerce=False)
    shard_ids = [r["shard_id"] for r in stored]
    assert len(stored) == 4 + 4 * 12 + 4
    assert len(shard_ids) == len(set(shard_ids)), "a Shard id was reused"


# ---------------------------------------------------------------------------
# Cost no longer grows with the history
# ---------------------------------------------------------------------------


def _best(fn, rounds: int = 15) -> float:
    best = float("inf")
    for _ in range(rounds):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def _history_of(root: Path, records: int) -> Path:
    filler = json.dumps({"receipt_id": "old", "executor": ch.EXECUTOR, "shard_id": "shard-old",
                         "capture": {"session_id": "old"}, "events": [{"n": i, "pad": "x" * 40} for i in range(300)]})
    runs = root / ".openshard" / "runs.jsonl"
    runs.parent.mkdir(parents=True)
    body = "".join(filler.replace('"old"', f'"old{i}"') + "\n" for i in range(records))
    recent = json.dumps(_session("recent", "sess-recent", pad=20)) + "\n"
    runs.write_text(body + recent + "".join(filler.replace('"old"', f'"tail{i}"') + "\n" for i in range(3)))
    return runs


def test_session_and_seal_lookups_cost_stays_flat_as_history_grows(tmp_path: Path) -> None:
    """A ratio of best-of-N timings, not absolute ms, so parallel load does not flake it.

    Before, both lookups parsed every line: the ~20 MB history cost ~40x the
    ~0.5 MB one. Now a recent record is found reading only the file's end.
    """
    costs = []
    for name, records in (("small", 20), ("large", 800)):  # ~0.5 MB and ~20 MB
        runs = _history_of(tmp_path / name, records)
        assert ch._find_persisted_entry(runs.parents[1], "sess-recent")["name"] == "recent"
        costs.append(_best(lambda runs=runs: (ch._find_persisted_entry(runs.parents[1], "sess-recent"),
                                              rs._find_stored(runs, _r("recent")))))
    small, large = costs
    assert large < max(small * 4, small + 0.01), f"small {small * 1e3:.2f} ms, large {large * 1e3:.2f} ms"


def test_new_shard_id_is_far_cheaper_than_parsing_the_history(tmp_path: Path) -> None:
    """Minting must check every line (an id is never reused), so it reads the
    whole file, but by byte scan: compared with a parse of the same file, now."""
    runs = _history_of(tmp_path, 800)
    mint = _best(lambda: ch._finalize_hook_shard_id(_new_receipt("n"), {}, _locked_lines(runs), None), 7)
    parse = _best(lambda: load_history(runs, coerce=False), 7)
    assert mint < parse / 3, f"mint {mint * 1e3:.2f} ms, parse {parse * 1e3:.2f} ms"
