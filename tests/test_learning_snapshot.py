"""The precomputed learning snapshot: same semantics as Learning Loop V1, none of its startup cost.

Covers the low-latency backend under the unchanged V1 surfaces: parity with
``derive_signals`` / ``load_scoped_history``, a startup path that never reads
history, truthful statuses (unknown is never zero), complete-only
publication, repository identity, Windows-safe reads, worker coalescing and
self-healing, and one frozen snapshot per OSN run with model-specific context.

OSN tests that assert on learning *data* use ``generous_learning_budget`` so a
busy machine cannot turn them into timeouts; the production 25 ms budget has
its own end-to-end test.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from openshard.history import jsonl_store
from openshard.history.receipt_evidence import learning_block
from openshard.history.shard_contract import build_shard_receipt, render_full_shard_receipt
from openshard.learning import snapshot as snap
from openshard.learning import worker
from openshard.learning.record import build_learning_record
from openshard.learning.retrieval import consult
from openshard.learning.routing import ScopedHistoryEvidence, load_scoped_history
from openshard.learning.signals import load_learning_index
from openshard.osn.routing import HARNESS
from openshard.routing.adaptive.history_evidence import load_history_evidence
from openshard.sync.envelope import build_envelope
from openshard.util.home import openshard_home
from tests.conftest import NO_SUCH_PID
from tests.learning_fixtures import MOBILE_CHECK, osn_entry, publish_learning
from tests.test_learning_osn import RecordingProvider, _invoke, _repo, _runs, _writes

TASK = "Update the dashboard analytics layout"
generous = pytest.mark.usefixtures("generous_learning_budget")


def _history(repo_name="shop"):
    t = "Fix responsive dashboard layout"
    return [
        osn_entry(t, attempts=[("fake/a", "failed"), ("fake/b", "passed")], check=MOBILE_CHECK,
                  repo=repo_name, days_ago=0.5),
        osn_entry(t, attempts=[("fake/a", "failed"), ("fake/b", "passed")], check=MOBILE_CHECK,
                  repo=repo_name, days_ago=0.4),
        osn_entry("Dashboard layout grid", attempts=[("fake/b", "passed")], check=MOBILE_CHECK,
                  repo=repo_name, days_ago=0.3),
        osn_entry("Dashboard layout cards", attempts=[("fake/a", "passed")], repo=repo_name, days_ago=0.2),
        osn_entry("Dashboard layout spacing", attempts=[("fake/a", "passed")], repo=repo_name, days_ago=0.1),
    ]


def _seeded(tmp_path, entries=None, name="shop") -> Path:
    repo = _repo(tmp_path) if name == "shop" else _named_repo(tmp_path, name)
    store = repo / ".openshard"
    store.mkdir()
    jsonl_store.write_jsonl(store / "runs.jsonl", entries if entries is not None else _history())
    publish_learning(repo)
    return repo


def _named_repo(tmp_path, name) -> Path:
    r = tmp_path / name
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    (r / "out.txt").write_text("bad")
    return r


def _lookup(repo, **kw) -> snap.LearningSnapshot:
    kw.setdefault("budget_ms", 5000)
    return snap.lookup_snapshot(repo / ".openshard", **kw)


def _runs_path(repo) -> Path:
    return repo / ".openshard" / "runs.jsonl"


# --- Same semantics as V1 ----------------------------------------------------


def test_snapshot_retrieval_matches_v1_derivation(tmp_path):
    repo = _seeded(tmp_path)
    found = _lookup(repo)
    assert found.status == "available" and found.repo == "shop"
    upstream = load_learning_index(repo, repo="shop")
    assert found.index.signals_total == len(upstream.signals)
    assert {s.signal_id for s in found.index.signals} == {s.signal_id for s in upstream.signals if s.surfaceable}
    for model in (None, "fake/a", "fake/b"):
        ours = found.consult(TASK, current_check_fingerprint="x", model=model)
        theirs = consult(TASK, upstream, repo="shop", current_check_fingerprint="x", model=model)
        assert ours.status == theirs.status
        assert [r.to_record() for r in ours.retrieved] == [r.to_record() for r in theirs.retrieved]
        assert ours.signals_considered == theirs.signals_considered
        assert ours.prompt_text == theirs.prompt_text


def test_freshness_is_recomputed_when_read_not_frozen_at_derive_time(tmp_path):
    repo = _seeded(tmp_path)
    assert _lookup(repo).consult(TASK).used
    later = datetime.now(UTC) + timedelta(days=120)  # every signal is now stale
    aged = _lookup(repo, now=later)
    ctx = aged.consult(TASK)
    assert aged.status == "available"
    assert ctx.status == "no_relevant_signals" and not ctx.retrieved  # signals exist, none current
    assert ctx.signals_considered == aged.index.signals_total > 0


def test_routing_history_matches_the_v1_scoped_and_harness_wide_loaders(tmp_path):
    entries = []
    for model, passed in (("acme/mid-1", False), ("zeta/mid-2", True)):
        for _ in range(5):
            entries.append(osn_entry("write ok into out.txt", attempts=[(model, "passed" if passed else "failed")],
                                     repo="shop", category="standard", days_ago=1))
    repo = _seeded(tmp_path, entries)
    runs = _runs_path(repo)
    found = _lookup(repo)
    candidates = ["acme/mid-1", "zeta/mid-2", "other/x"]
    for category in ("standard", "visual", None):
        ours = found.history(category, harness=HARNESS)
        theirs = load_scoped_history(runs, harness=HARNESS, repo="shop", task_category=category)
        assert type(ours) is type(theirs)
        evidence, record = ours.gate(candidates)
        expected_evidence, expected = theirs.gate(candidates)
        record.pop("snapshot_id", None)
        assert evidence == expected_evidence and record == expected
    broad, expected = found.history(None, harness=HARNESS), load_history_evidence(runs, harness=HARNESS)
    assert broad.per_model == expected.per_model and broad.entries_scanned == expected.entries_scanned


def test_startup_lookup_reads_no_history_runs_no_git_and_makes_no_remote_call(tmp_path, monkeypatch):
    repo = _seeded(tmp_path)
    forbidden = Mock(side_effect=AssertionError("startup must not read history, run git or call out"))
    for target in (
        "openshard.learning.signals.read_entries", "openshard.learning.signals.load_learning_index",
        "openshard.learning.signals.derive_signals", "openshard.learning.signals.repo_key",
        "openshard.learning.routing.load_scoped_history",
        "openshard.routing.adaptive.history_evidence.load_history_evidence",
        "openshard.history.repo_identity.capture_repo_identity",
        "openshard.util.git.run_git",
    ):
        monkeypatch.setattr(target, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    _runs_path(repo).unlink()  # the snapshot stands alone
    found = _lookup(repo)
    assert found.status == "available"
    assert found.consult(TASK).used
    assert isinstance(found.history("visual", harness=HARNESS), ScopedHistoryEvidence)


# --- Truthful statuses: unknown is never zero -----------------------------------


def _write_raw(repo, value) -> None:
    path = snap.snapshot_path(repo / ".openshard")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def _published(repo) -> dict:
    return json.loads(snap.snapshot_path(repo / ".openshard").read_text(encoding="utf-8"))


@pytest.mark.parametrize("damage, status", [
    ("missing", "missing"),
    ("corrupt", "corrupt"),
    ("incomplete", "incomplete"),
    ("incompatible", "incompatible"),
    ("oversized", "oversized"),
    ("bad_signal", "corrupt"),
    ("bad_routing", "corrupt"),
    ("no_identity", "repo_mismatch"),
])
def test_unusable_snapshots_are_unavailable_never_no_history(tmp_path, damage, status):
    repo = _seeded(tmp_path)
    good = _published(repo)
    if damage == "missing":
        snap.snapshot_path(repo / ".openshard").unlink()
    elif damage == "corrupt":
        _write_raw(repo, '{"format": "openshard-learning-snapshot", ')
    elif damage == "incomplete":
        _write_raw(repo, {**good, "complete": False})
    elif damage == "incompatible":
        _write_raw(repo, {**good, "version": 999})
    elif damage == "oversized":
        _write_raw(repo, {**good, "availability": "oversized"})
    elif damage == "bad_signal":
        _write_raw(repo, {**good, "signals": [{"kind": "model_task_outcomes", "signal_id": 3}]})
    elif damage == "no_identity":
        _write_raw(repo, {**good, "identity": None})
    else:
        _write_raw(repo, {**good, "routing": {**good["routing"], "broad": {"models": [["m", -1, 0, 0, None]]}}})
    found = _lookup(repo)
    assert found.status == status
    ctx = found.consult(TASK)
    assert ctx.status == "unavailable" and not ctx.retrieved and ctx.prompt_text is None
    assert ctx.signals_considered is None and ctx.receipts_with_evidence is None
    _, record = found.history("visual", harness=HARNESS).gate(["fake/a", "fake/b"])
    assert record["reason"] == "history_unavailable" and record["used"] is False
    assert record["entries_scanned"] is None and record["candidates_with_evidence"] is None


def test_no_snapshot_and_no_history_is_genuinely_no_history(tmp_path):
    repo = _repo(tmp_path)
    found = _lookup(repo)
    assert found.status == "no_history"
    ctx = found.consult(TASK)
    assert ctx.status == "no_history" and ctx.signals_considered == 0  # known empty: zero is true here
    _, record = found.history("visual", harness=HARNESS).gate(["fake/a"])
    assert record["reason"] == "no_history" and record["entries_scanned"] == 0


def test_zero_budget_is_a_timeout_and_reads_nothing(tmp_path, monkeypatch):
    repo = _seeded(tmp_path)
    monkeypatch.setattr(snap, "_read", Mock(side_effect=AssertionError("must not read")))
    found = snap.lookup_snapshot(repo / ".openshard", budget_ms=0)
    ctx = found.consult(TASK)
    assert found.status == "timeout" and ctx.status == "timeout"
    assert ctx.signals_considered is None and ctx.receipts_with_evidence is None
    _, record = found.history("visual", harness=HARNESS).gate(["fake/a"])
    assert record["reason"] == "history_timeout" and record["verified_outcomes_for_candidates"] is None


@pytest.mark.parametrize("lookup_status, status, wording", [
    ("timeout", "timeout", "not used: the bounded learning lookup did not finish in time"),
    ("corrupt", "unavailable", "not used: learning evidence could not be read"),
    ("missing", "unavailable", "not used: learning snapshot not built yet"),
    ("oversized", "unavailable", "not used: learning snapshot exceeded the size cap"),
])
def test_unknown_counts_stay_unknown_through_record_projection_and_render(lookup_status, status, wording):
    """LearningContext -> canonical Receipt record -> projected block -> Markdown: never 0."""
    lookup = snap.LearningSnapshot(lookup_status, 3.0, 25.0)
    ctx = lookup.consult(TASK)
    assert ctx.status == status and ctx.signals_considered is None and ctx.receipts_with_evidence is None
    record = build_learning_record(ctx, check=None, snapshot=lookup.record())
    assert record["status"] == status and record["signals_considered"] is None
    assert record["used"] is False and record["signals_used"] == 0  # nothing reached a model: true
    entry = osn_entry(TASK, learning=record)
    block = learning_block(entry)
    assert block["status"] == status and block["signals_considered"] is None
    assert block["snapshot"]["status"] == lookup.status
    assert '"learning"' not in json.dumps(build_envelope(entry, 0, core_version="0.5.0"))  # stays local
    text = render_full_shard_receipt(build_shard_receipt(entry, index=0))
    section = text.split("LEARNING", 1)[1].split("\n\n", 1)[0]
    signals_row = next(line for line in section.splitlines() if line.strip().startswith("Signals"))
    assert signals_row.strip().endswith(wording)
    assert "considered" not in section and " 0 " not in section


def test_a_stalled_read_is_bounded_and_never_accumulates_readers(tmp_path, monkeypatch):
    repo = _seeded(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def stall(*_a):
        entered.set()
        release.wait(2)
        raise OSError("slow filesystem")

    monkeypatch.setattr(snap, "_read", stall)
    start = time.perf_counter()
    try:
        first = snap.lookup_snapshot(repo / ".openshard", budget_ms=10)
        assert time.perf_counter() - start < 0.5  # bounded by the budget, scheduler-tolerant
        assert first.status == "timeout" and entered.is_set()
        assert snap.lookup_snapshot(repo / ".openshard", budget_ms=10).status == "busy"
    finally:
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:  # let the stalled reader free the slot for later tests
            if snap._reader_slot.acquire(blocking=False):
                snap._reader_slot.release()
                break
            time.sleep(0.001)


def test_a_lookup_waits_within_its_budget_for_an_earlier_read_to_free_the_slot(tmp_path):
    repo = _seeded(tmp_path)
    assert snap._reader_slot.acquire(blocking=False)
    timer = threading.Timer(0.05, snap._reader_slot.release)
    timer.start()
    try:
        found = snap.lookup_snapshot(repo / ".openshard", budget_ms=5000)
        assert found.status == "available"
    finally:
        timer.join()


def test_a_result_arriving_after_the_budget_is_not_used(tmp_path, monkeypatch):
    repo = _seeded(tmp_path)
    real = snap._read

    def slow(path, now):
        value = real(path, now)
        time.sleep(0.05)
        return value

    monkeypatch.setattr(snap, "_read", slow)
    found = snap.lookup_snapshot(repo / ".openshard", budget_ms=20)
    assert found.status == "timeout" and found.index is None


def test_lookup_budget_config_defaults_caps_and_never_turns_invalid_into_zero():
    assert snap.DEFAULT_BUDGET_MS == 25.0 and snap.MAX_BUDGET_MS == 100.0
    assert snap.lookup_budget_ms({}) == 25
    assert snap.lookup_budget_ms({"learning": {"lookup_budget_ms": 60}}) == 60
    assert snap.lookup_budget_ms({"learning": {"lookup_budget_ms": 10_000}}) == 100
    assert snap.lookup_budget_ms({"learning": {"lookup_budget_ms": -5}}) == 0
    for bad in ("fast", True, float("nan"), float("inf"), None, 10**1000):
        assert snap.lookup_budget_ms({"learning": {"lookup_budget_ms": bad}}) == 25


# --- Repository identity --------------------------------------------------------


def test_a_snapshot_copied_into_another_checkout_is_never_used(tmp_path):
    shop = _seeded(tmp_path)
    other = _named_repo(tmp_path, "other")
    (other / ".openshard").mkdir()
    jsonl_store.write_jsonl(_runs_path(other), [osn_entry(repo="other")])
    shutil.copytree(shop / ".openshard" / "learning-cache", other / ".openshard" / "learning-cache")
    found = _lookup(other)
    assert found.status == "repo_mismatch" and found.repo is None
    ctx = found.consult(TASK)
    assert ctx.status == "unavailable" and not ctx.retrieved and ctx.signals_considered is None
    assert found.history("visual", harness=HARNESS).gate(["fake/a"])[1]["reason"] == "history_unavailable"
    assert _lookup(shop).status == "available"  # the original checkout is unaffected


def test_a_changed_remote_invalidates_the_snapshot_until_rederived(tmp_path):
    repo = _seeded(tmp_path)
    assert _lookup(repo).status == "available"
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/elsewhere.git"],
                   cwd=repo, check=True)
    found = _lookup(repo)
    assert found.status == "identity_changed"
    assert found.consult(TASK).status == "unavailable"
    publish_learning(repo)  # the worker re-derives with the new identity
    rederived = _lookup(repo)
    assert rederived.status == "available" and rederived.repo == "github.com/acme/elsewhere"


def test_a_checkout_that_gained_a_git_dir_is_rechecked(tmp_path):
    plain = tmp_path / "plain"
    (plain / ".openshard").mkdir(parents=True)
    jsonl_store.write_jsonl(_runs_path(plain), _history())
    snapshot = snap.build_snapshot([json.dumps(e) + "\n" for e in _history()], repo="plain", harness=HARNESS,
                                   identity={"root": snap._root_key(plain), "git_dir_present": False,
                                             "git_config": None, "git_config_stat": None})
    snap.publish_snapshot(plain / ".openshard", snapshot)
    assert _lookup(plain).status == "available"
    (plain / ".git").mkdir()
    assert _lookup(plain).status == "identity_changed"


# --- Complete snapshots only, sized for the budget --------------------------------


def test_a_snapshot_is_published_whole_from_one_multi_chunk_copy(tmp_path, monkeypatch):
    entries = [osn_entry("Dashboard layout grid", attempts=[("fake/b", "passed")], repo="shop",
                         files=(f"src/dash/{'x' * 200}{i}.tsx",)) for i in range(120)]
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    jsonl_store.write_jsonl(runs, entries)
    monkeypatch.setattr(worker, "CHUNK_BYTES", 4096)  # many chunks, each under the lock
    published: list[dict] = []
    real_publish = snap.publish_snapshot
    monkeypatch.setattr(snap, "publish_snapshot", lambda store, s: published.append(s) or real_publish(store, s))
    worker.refresh_snapshot(runs)
    assert len(published) == 1  # one publish per pass, after the whole copy
    assert published[0]["index"]["receipts_observed"] == 120
    assert published[0]["source"] == snap.stat_key(runs)
    assert _lookup(repo).index.receipts_observed == 120


def test_a_copy_restarts_when_history_is_replaced_between_chunks(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    jsonl_store.write_jsonl(runs, [{"n": i, "pad": "a" * 300} for i in range(40)])
    replacement = [{"n": i, "pad": "b" * 300} for i in range(30)]
    monkeypatch.setattr(worker, "CHUNK_BYTES", 2048)
    real_lock, calls = worker.history_file_lock, []

    def lock(path, **kw):
        calls.append(path)
        if len(calls) == 2:  # between the first and second chunk, a writer rewrites history
            jsonl_store.write_jsonl(runs, replacement)
        return real_lock(path, **kw)

    monkeypatch.setattr(worker, "history_file_lock", lock)
    data = worker.copy_history(runs)
    assert data == runs.read_bytes()  # never a mix of old and new
    lines = data.decode("utf-8").splitlines()
    assert '"b' in lines[0] and len(lines) == 30


def test_a_snapshot_that_cannot_fit_even_without_signals_is_a_truthful_marker(tmp_path, monkeypatch):
    repo = _seeded(tmp_path)
    monkeypatch.setattr(snap, "MAX_SNAPSHOT_BYTES", 64)  # below any snapshot, even one with no signals
    worker.refresh_snapshot(_runs_path(repo))
    assert _published(repo)["availability"] == "oversized"
    assert _lookup(repo).status == "oversized"
    assert not list((repo / ".openshard" / "learning-cache").glob("*.tmp"))


def _heavy_history(n: int, repo_name: str) -> list[dict]:
    """A long, varied history: many models, categories and checks, as a busy team accumulates."""
    import random

    rnd = random.Random(11)
    words = ["dashboard", "layout", "api", "auth", "billing", "search", "upload", "cache", "queue", "report",
             "export", "login", "grid", "chart", "worker", "parser", "schema", "router", "table", "form"]
    categories = ["visual", "backend", "testing", "refactor", "docs", "security", "performance", "infra"]
    models = [f"vendor{i % 5}/model-{i}" for i in range(24)]
    checks = [{"fingerprint": f"{i:016x}", "label": f"pytest tests/test_{words[i % 20]}_{i}.py",
               "label_complete": True, "kind": "test"} for i in range(60)]
    return [
        osn_entry(" ".join(rnd.sample(words, 3)) + f" fix {i % 97}",
                  attempts=[(rnd.choice(models), rnd.choice(["failed", "passed", "timeout"])),
                            (rnd.choice(models), "passed")],
                  check=rnd.choice(checks), repo=repo_name, category=rnd.choice(categories),
                  days_ago=rnd.random() * 25, files=(f"src/{rnd.choice(words)}/{rnd.choice(words)}{i % 50}.py",))
        for i in range(n)
    ]


def test_a_large_history_is_trimmed_to_fit_not_dropped(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    jsonl_store.write_jsonl(runs, _heavy_history(2500, "shop"))
    lines = runs.read_text(encoding="utf-8").splitlines(keepends=True)
    full = snap.build_snapshot(lines, repo="shop", harness=HARNESS)
    assert len(snap._encode(full)) > snap.MAX_SNAPSHOT_BYTES  # the cliff this guards against

    worker.refresh_snapshot(runs)
    published = _published(repo)
    size = snap.snapshot_path(repo / ".openshard").stat().st_size
    assert published["availability"] == "available" and size <= snap.MAX_SNAPSHOT_BYTES
    meta = published["index"]
    assert meta["trimmed"] is True and meta["signals_total"] == full["index"]["signals_total"]
    assert meta["signals_stored"] == len(published["signals"]) < len(full["signals"])
    # The strongest and most recent survive: nothing dropped outranks anything kept.
    kept = {s["signal_id"] for s in published["signals"]}
    ranks = {s["signal_id"]: snap._keep_rank(s) for s in full["signals"]}
    assert max(ranks[i] for i in kept) <= min(r for i, r in ranks.items() if i not in kept)
    assert [s["signal_id"] for s in published["signals"]] == [
        s["signal_id"] for s in full["signals"] if s["signal_id"] in kept]  # derived order kept
    # Deterministic: the same derivation trims to the same bytes.
    assert snap.fit_snapshot(full) == snap.fit_snapshot(json.loads(json.dumps(full)))

    assert meta["signals_dropped"] == len(full["signals"]) - meta["signals_stored"]

    found = _lookup(repo)
    assert found.status == "available" and found.trimmed
    assert len(found.index.signals) == meta["signals_stored"] == found.signals_stored
    assert found.signals_derived == meta["signals_total"]
    # Considered: what could be scored. Dropped signals never were, so they are not counted.
    considered = meta["signals_total"] - meta["signals_dropped"]
    assert found.consult(TASK).signals_considered == considered < meta["signals_total"]
    record = found.record()
    assert record["trimmed"] is True and record["signals_stored"] == meta["signals_stored"]
    assert record["signals_derived"] == meta["signals_total"]

    # Through the Receipt: record -> projection -> rendered text.
    ctx = found.consult(TASK)
    learning = build_learning_record(ctx, check=None, snapshot=record)
    assert learning["signals_considered"] == considered
    entry = osn_entry(TASK, learning=learning)
    block = learning_block(entry)
    assert block["signals_considered"] == considered
    assert {k: block["snapshot"][k] for k in ("trimmed", "signals_stored", "signals_derived")} == {
        "trimmed": True, "signals_stored": meta["signals_stored"], "signals_derived": meta["signals_total"]}
    text = render_full_shard_receipt(build_shard_receipt(entry, index=0))
    section = text.split("LEARNING", 1)[1].split("\n\n", 1)[0]
    assert (f"{meta['signals_stored']} of {meta['signals_total']} derived signal(s) stored (trimmed to fit)"
            in section) == (learning["status"] in ("used", "no_relevant_signals"))


def test_an_untrimmed_snapshot_records_no_trim_fields(tmp_path):
    repo = _seeded(tmp_path)
    found = _lookup(repo)
    assert not found.trimmed and "trimmed" not in found.record()
    assert "trimmed" not in learning_block(osn_entry(TASK, learning=build_learning_record(
        found.consult(TASK), check=None, snapshot=found.record())))["snapshot"]


def test_a_trimmed_snapshot_with_inconsistent_counts_is_corrupt(tmp_path):
    repo = _seeded(tmp_path)
    bad = _published(repo)
    bad.pop("snapshot_id")
    bad["index"].update(trimmed=True, signals_stored=len(bad["signals"]) + 1, signals_dropped=1)
    snap.publish_snapshot(repo / ".openshard", bad)
    assert _lookup(repo).status == "corrupt"


def test_routing_history_is_unavailable_when_the_live_loader_could_not_decode_it(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    jsonl_store.write_jsonl(runs, _history())
    with open(runs, "ab") as fh:
        fh.write(b'{"receipt_id": "bad", "task": "caf\xe9"}\n')  # not UTF-8
    live = None
    try:
        live = load_history_evidence(runs, harness=HARNESS)
    except UnicodeDecodeError:
        pass  # the live loader raises; routing then uses no history
    assert live is None
    publish_learning(repo)
    found = _lookup(repo)
    assert found.status == "available" and found.index.receipts_observed >= 5  # signals: lenient, as live
    history = found.history("visual", harness=HARNESS)
    assert history.availability == "unavailable"
    _, record = history.gate(["fake/a"])
    assert record["reason"] == "history_unavailable"


def test_scoped_history_is_bounded_and_a_dropped_category_falls_back_to_harness_wide(tmp_path, monkeypatch):
    monkeypatch.setattr(snap, "MAX_SCOPED_CATEGORIES", 2)
    entries = [osn_entry(category=c, repo="shop", days_ago=0.1) for c in ("visual", "visual", "backend", "docs")]
    lines = [json.dumps(e) + "\n" for e in entries]
    built = snap.build_snapshot(lines, repo="shop", harness=HARNESS)
    assert set(built["routing"]["scoped"]) == {"visual", "backend"} or set(built["routing"]["scoped"]) == {
        "visual", "docs"}
    assert built["routing"]["scoped_truncated"] is True


def test_a_version_1_snapshot_is_incompatible_and_rebuilt(tmp_path, real_learning_scheduler):
    repo = _seeded(tmp_path)
    _settled(repo, real_learning_scheduler)
    old = _published(repo)
    old.pop("snapshot_id")
    old["version"] = 1
    snap.publish_snapshot(repo / ".openshard", old)
    found = _lookup(repo)
    assert found.status == "incompatible" and found.learning_status == "unavailable"
    assert found.consult(TASK).signals_considered is None  # unknown, not zero
    assert worker.nudge(_runs_path(repo), found) is True and len(real_learning_scheduler) == 1


def test_stored_freshness_is_epoch_based_and_matches_the_v1_rule(tmp_path):
    repo = _seeded(tmp_path)
    published = _published(repo)
    for raw in published["signals"]:
        assert "freshness" not in raw and isinstance(raw["last_seen_epoch"], int)
        assert raw["last_seen_epoch"] == int(datetime.fromisoformat(raw["last_seen"].replace("Z", "+00:00"))
                                             .timestamp())
    base = datetime.fromisoformat(published["signals"][0]["last_seen"].replace("Z", "+00:00"))
    for days in (0, 30, 30.5, 31, 90, 91, 200):
        now = base + timedelta(days=days)
        found = _lookup(repo, now=now)
        from openshard.learning.signals import freshness_for, parse_timestamp

        for sig in found.index.signals:
            assert sig.freshness == freshness_for(parse_timestamp(sig.last_seen), now)


@pytest.mark.parametrize("damage", [
    lambda s: s.pop("last_seen_epoch"),
    lambda s: s.update(last_seen_epoch="2026-01-01"),
    lambda s: s.update(last_seen_epoch=True),
    lambda s: s.update(terms=["ok", 3]),
    lambda s: s.update(receipt_ids="r1"),
    lambda s: s.update(areas=["x" * 2000]),
    lambda s: s.update(repo=7),
    lambda s: s.update(samples=True),
    lambda s: s.pop("shard_ids"),
])
def test_a_malformed_stored_signal_makes_the_snapshot_corrupt(tmp_path, damage):
    repo = _seeded(tmp_path)
    bad = _published(repo)
    bad.pop("snapshot_id")
    damage(bad["signals"][0])
    snap.publish_snapshot(repo / ".openshard", bad)
    assert _lookup(repo).status == "corrupt"


def _near_max_snapshot(repo) -> int:
    """Republish *repo*'s snapshot padded with distinct valid signals to ~95% of the cap."""
    good = _published(repo)
    good.pop("snapshot_id")
    base = good["signals"]
    per = len(json.dumps(base, separators=(",", ":"))) / len(base)
    n = int((snap.MAX_SNAPSHOT_BYTES * 0.95 - 8000) / per)
    good["signals"] = [{**base[i % len(base)], "signal_id": f"{base[i % len(base)]['signal_id']}_{i}"}
                       for i in range(n)]
    good["index"]["signals_total"] = n
    snap.publish_snapshot(repo / ".openshard", good)
    size = snap.snapshot_path(repo / ".openshard").stat().st_size
    assert 0.85 * snap.MAX_SNAPSHOT_BYTES < size <= snap.MAX_SNAPSHOT_BYTES
    return n


_FRESH_LOOKUP = """
import json, sys
from pathlib import Path
from openshard.learning.snapshot import lookup_snapshot
r = lookup_snapshot(Path(sys.argv[1]), budget_ms=float(sys.argv[2]))
print(json.dumps({"status": r.status, "lookup_ms": r.lookup_ms,
                  "total": r.index.signals_total if r.index else None}))
"""


def _fresh_lookup(repo, budget_ms) -> dict:
    out = subprocess.run([sys.executable, "-c", _FRESH_LOOKUP, str(repo / ".openshard"), str(budget_ms)],
                         capture_output=True, text=True, check=True, timeout=60)
    return json.loads(out.stdout)


def test_a_fresh_process_reads_a_snapshot_near_the_supported_maximum(tmp_path):
    repo = _seeded(tmp_path)
    n = _near_max_snapshot(repo)
    whole = _fresh_lookup(repo, 5000)  # completeness, independent of machine load
    assert whole["status"] == "available" and whole["total"] == n
    # The production budget: under load a lookup may be late, and then it must say
    # so -- never a partial or wrong answer.
    for _ in range(3):
        result = _fresh_lookup(repo, snap.DEFAULT_BUDGET_MS)
        assert result["status"] in ("available", "timeout")
        if result["status"] == "available":
            assert result["total"] == n and result["lookup_ms"] < snap.DEFAULT_BUDGET_MS
        else:
            assert result["total"] is None and result["lookup_ms"] >= snap.DEFAULT_BUDGET_MS


# --- Windows-safe reads and writes ---------------------------------------------


def test_history_is_closed_before_derivation_so_writers_can_replace_it(tmp_path, monkeypatch):
    """Simulates Windows: a rename over a file another handle holds open fails."""
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    jsonl_store.write_jsonl(runs, _history())
    opened = []
    real_open = Path.open

    def tracked_open(p, *args, **kwargs):
        fh = real_open(p, *args, **kwargs)
        if p == runs and args and args[0] == "rb":
            opened.append(fh)
        return fh

    real_replace = jsonl_store.os.replace

    def windows_replace(a, b):
        if Path(b) == runs and any(not f.closed for f in opened):
            raise PermissionError("sharing violation: history still open")
        real_replace(a, b)

    monkeypatch.setattr(Path, "open", tracked_open)
    monkeypatch.setattr(jsonl_store.os, "replace", windows_replace)
    paused, resume = threading.Event(), threading.Event()
    real_build = snap.build_snapshot

    def slow_build(*a, **kw):
        paused.set()
        assert resume.wait(5)
        return real_build(*a, **kw)

    monkeypatch.setattr(snap, "build_snapshot", slow_build)
    errors: list[BaseException] = []
    thread = threading.Thread(target=lambda: _capture(errors, worker.refresh_snapshot, runs))
    thread.start()
    try:
        assert paused.wait(5)
        assert opened and all(f.closed for f in opened)
        assert jsonl_store.upsert_jsonl(runs, {"receipt_id": "new"}, lambda e: False) == "appended"
        jsonl_store.write_jsonl(runs, _history())  # an atomic rewrite succeeds mid-derivation
    finally:
        resume.set()
        thread.join(5)
    assert not thread.is_alive() and not errors


def _capture(errors, fn, *args):
    try:
        fn(*args)
    except BaseException as exc:  # pragma: no cover - surfaced by the assertion
        errors.append(exc)


@pytest.mark.parametrize("windows", [False, True])
def test_replace_retries_only_transient_windows_sharing_violations(tmp_path, monkeypatch, windows):
    target = tmp_path / "f"
    target.write_text("old")
    source = tmp_path / "g"
    source.write_text("new")
    monkeypatch.setattr(jsonl_store.sys, "platform", "win32" if windows else "linux")
    real, failures = jsonl_store.os.replace, [PermissionError("busy")]

    def flaky(a, b):
        if failures:
            raise failures.pop()
        real(a, b)

    monkeypatch.setattr(jsonl_store.os, "replace", flaky)
    if windows:
        jsonl_store.replace_with_retry(source, target)
        assert target.read_text() == "new"
    else:
        with pytest.raises(PermissionError):
            jsonl_store.replace_with_retry(source, target)


def test_a_reader_outlasting_the_replace_retry_is_absorbed_by_the_pass_retry(tmp_path, monkeypatch):
    """A lookup holding the snapshot open longer than the 6 ms replace retry: the worker
    retries the whole pass with backoff and publishes, with no new history write needed."""
    repo = _seeded(tmp_path)
    before = _published(repo)["snapshot_id"]
    jsonl_store.append_jsonl(_runs_path(repo), osn_entry("Dashboard layout footer", repo="shop", days_ago=0.05))
    real, blocked = jsonl_store.os.replace, [0]  # the production pass retry, unpatched
    limit = 6 if sys.platform == "win32" else 2  # more than one replace_with_retry absorbs

    def held_by_reader(a, b):
        if Path(b).name == snap.SNAPSHOT_NAME and blocked[0] < limit:
            blocked[0] += 1
            raise PermissionError("sharing violation: a lookup holds the snapshot")
        real(a, b)

    monkeypatch.setattr(jsonl_store.os, "replace", held_by_reader)
    assert worker.update_snapshot(_runs_path(repo)) is True
    after = _published(repo)
    assert blocked[0] == limit and after["snapshot_id"] != before
    assert after["index"]["receipts_observed"] == 6
    assert not list((repo / ".openshard" / "learning-cache").glob("*.tmp"))


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows sharing semantics")
def test_a_real_windows_reader_holding_the_snapshot_does_not_strand_the_update(tmp_path, monkeypatch):
    """A real open handle: the replace genuinely fails until the reader closes it."""
    repo = _seeded(tmp_path)
    before = _published(repo)["snapshot_id"]
    jsonl_store.append_jsonl(_runs_path(repo), osn_entry("Dashboard layout footer", repo="shop", days_ago=0.05))
    fh = snap.snapshot_path(repo / ".openshard").open("rb")  # a lookup mid-read
    real, refused = jsonl_store.os.replace, []

    def replace(a, b):
        try:
            real(a, b)
        except PermissionError:
            if not refused:  # the reader lets go only after the whole replace retry has failed
                threading.Timer(0.03, fh.close).start()
            refused.append(1)
            raise

    monkeypatch.setattr(jsonl_store.os, "replace", replace)
    try:
        assert worker.update_snapshot(_runs_path(repo)) is True
    finally:
        fh.close()
    assert len(refused) >= 4  # a full replace_with_retry was exhausted: the pass retry recovered
    assert _published(repo)["snapshot_id"] != before


# --- Background worker: scheduling, coalescing, recovery -------------------------


def test_the_real_scheduler_launches_one_detached_worker_with_repo_conventions(tmp_path, real_learning_scheduler):
    runs = tmp_path / "runs.jsonl"
    jsonl_store.append_jsonl(runs, {"receipt_id": "r1"})
    assert len(real_learning_scheduler) == 1
    argv, kwargs = real_learning_scheduler[0]
    token = (tmp_path / worker.LAUNCH).read_text(encoding="ascii").split(" ")[0]
    # -P: ``-m`` must not put the working directory on sys.path (a repository could ship
    # its own ``openshard`` package there), and the working directory is not the repository.
    assert argv == [sys.executable, "-P", "-m", "openshard.learning.worker", str(runs.resolve()), token]
    assert kwargs["cwd"] == openshard_home() and Path(kwargs["cwd"]).is_dir()
    assert not Path(kwargs["cwd"]).resolve().is_relative_to(tmp_path.resolve())
    assert kwargs["close_fds"] is True
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    if sys.platform == "win32":
        flags = kwargs["creationflags"]
        assert flags == (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                         | subprocess.CREATE_BREAKAWAY_FROM_JOB)  # normal priority
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True and "creationflags" not in kwargs
    assert (tmp_path / worker.LAUNCH).read_text(encoding="ascii") == f"{token} {real_learning_scheduler.pid}"


def test_the_cwd_falls_back_to_the_interpreter_directory_never_the_repository(tmp_path, monkeypatch):
    blocker = tmp_path / "home-is-a-file"
    blocker.write_text("")
    monkeypatch.setenv("OPENSHARD_HOME", str(blocker / "sub"))  # cannot be created
    assert worker._detached_kwargs()["cwd"] == os.path.dirname(os.path.abspath(sys.executable))


@pytest.mark.parametrize("job_allows_breakaway", [True, False])
def test_breakaway_is_requested_and_dropped_only_when_the_job_forbids_it(monkeypatch, job_allows_breakaway):
    calls = []

    def popen(argv, **kwargs):
        calls.append(kwargs["creationflags"])
        if kwargs["creationflags"] & worker.CREATE_BREAKAWAY_FROM_JOB and not job_allows_breakaway:
            raise PermissionError(5, "Access is denied")
        return "proc"

    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    base = 0x08000000 | 0x00000200
    assert worker._spawn(["x"], creationflags=base | worker.CREATE_BREAKAWAY_FROM_JOB) == "proc"
    if job_allows_breakaway:
        assert calls == [base | worker.CREATE_BREAKAWAY_FROM_JOB]
    else:
        assert calls == [base | worker.CREATE_BREAKAWAY_FROM_JOB, base]  # one retry, same other flags
    assert worker.CREATE_BREAKAWAY_FROM_JOB == 0x01000000


def test_a_spawn_without_breakaway_is_not_retried(monkeypatch):
    popen = Mock(side_effect=FileNotFoundError("python"))
    monkeypatch.setattr(worker.subprocess, "Popen", popen)
    with pytest.raises(FileNotFoundError):
        worker._spawn(["x"], start_new_session=True)
    assert popen.call_count == 1


def _plant(repo, marker) -> None:
    for where in (repo / ".openshard" / "openshard", repo / "openshard"):
        where.mkdir(parents=True, exist_ok=True)
        (where / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")


@pytest.mark.parametrize("defence", ["safe_path_only", "trusted_cwd_only", "neither"])
def test_each_defence_alone_blocks_a_planted_module(tmp_path, defence):
    """-P alone (cwd = the repository's store) and the trusted cwd alone (no -P) each keep
    the planted package off the import path; with neither, it would run (the control)."""
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    with open(runs, "w", encoding="utf-8") as fh:  # no scheduler: this test launches the worker itself
        fh.write("".join(json.dumps(e) + "\n" for e in _history()))
    marker = tmp_path / "INJECTED"
    _plant(repo, marker)
    argv = worker.worker_argv(runs, "tok")
    assert argv[1] == "-P"
    if defence != "safe_path_only":
        argv = [argv[0], *argv[2:]]
    cwd = worker._trusted_cwd() if defence == "trusted_cwd_only" else str(repo / ".openshard")
    env = {**os.environ, worker.ENV_SWITCH: "0"}
    subprocess.run(argv, cwd=cwd, env=env, timeout=120, capture_output=True)
    assert marker.exists() is (defence == "neither")
    if defence != "neither":
        assert _lookup(repo).status == "available"  # the real worker ran and published


def test_the_trusted_cwd_is_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENSHARD_HOME", "relative-home")
    cwd = worker._trusted_cwd()
    assert os.path.isabs(cwd) and cwd == str(tmp_path / "relative-home")


def test_a_repository_cannot_inject_code_into_the_real_worker(tmp_path, monkeypatch):
    """A cloned repository shipping ``.openshard/openshard/__init__.py`` (or ``openshard/``
    at its root) must never be imported by the worker a history write launches."""
    monkeypatch.delenv(worker.ENV_SWITCH, raising=False)
    repo = _repo(tmp_path)
    store = repo / ".openshard"
    marker = tmp_path / "INJECTED"
    for where in (store / "openshard", repo / "openshard"):
        where.mkdir(parents=True)
        (where / "__init__.py").write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    runs = _runs_path(repo)
    jsonl_store.write_jsonl(runs, _history())  # the real scheduler: a real detached worker
    found = _wait_for_published(repo)
    assert found.status == "available" and found.index.receipts_observed == 5  # the worker ran
    assert not marker.exists()


def _wait_for_published(repo) -> snap.LearningSnapshot:
    deadline = time.monotonic() + 60
    found = _lookup(repo)
    while time.monotonic() < deadline:
        found = _lookup(repo)
        if found.status == "available" and not (repo / ".openshard" / worker.LAUNCH).exists():
            break
        time.sleep(0.1)
    deadline = time.monotonic() + 30  # let the worker release its owner lock before tmp cleanup
    while time.monotonic() < deadline:
        try:
            with jsonl_store.history_file_lock(repo / ".openshard" / worker.OWNER, timeout=0):
                break
        except TimeoutError:
            time.sleep(0.05)
    return found


def test_a_burst_of_history_writes_launches_one_worker(tmp_path, real_learning_scheduler):
    runs = tmp_path / "runs.jsonl"
    threads = [threading.Thread(target=jsonl_store.append_jsonl, args=(runs, {"receipt_id": f"r{i}"}))
               for i in range(200)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runs.read_text().splitlines()) == 200
    assert len(real_learning_scheduler) == 1
    assert (tmp_path / "learning.launch").exists() and (tmp_path / "learning.dirty").exists()


def test_only_runs_jsonl_writes_schedule_learning(tmp_path, real_learning_scheduler):
    jsonl_store.append_jsonl(tmp_path / "interactions.jsonl", {"x": 1})
    assert real_learning_scheduler == [] and not (tmp_path / worker.DIRTY).exists()


def test_the_switch_turns_background_refresh_off(tmp_path, real_learning_scheduler, monkeypatch):
    monkeypatch.setenv(worker.ENV_SWITCH, "0")
    jsonl_store.append_jsonl(tmp_path / "runs.jsonl", {"receipt_id": "r1"})
    assert real_learning_scheduler == [] and not (tmp_path / worker.DIRTY).exists()


def test_writes_while_a_worker_owns_learning_launch_nothing(tmp_path, real_learning_scheduler):
    runs = tmp_path / "runs.jsonl"
    with jsonl_store.history_file_lock(tmp_path / worker.OWNER):
        for i in range(20):
            jsonl_store.append_jsonl(runs, {"receipt_id": f"r{i}"})
        assert worker.nudge(runs, snap.LearningSnapshot("missing", 1.0, 25.0)) is False
    assert real_learning_scheduler == []


def test_a_reservation_whose_process_died_is_reclaimed_at_once(tmp_path, real_learning_scheduler):
    runs = tmp_path / "runs.jsonl"
    (tmp_path / worker.LAUNCH).write_text(f"deadtoken {NO_SUCH_PID}", encoding="ascii")  # fresh, but dead
    jsonl_store.append_jsonl(runs, {"receipt_id": "r1"})
    assert len(real_learning_scheduler) == 1


def test_a_live_reservation_is_respected_but_only_for_a_bounded_time(tmp_path, real_learning_scheduler):
    runs = tmp_path / "runs.jsonl"
    launch = tmp_path / worker.LAUNCH
    launch.write_text(f"livetoken {os.getpid()}", encoding="ascii")
    jsonl_store.append_jsonl(runs, {"receipt_id": "r1"})
    assert real_learning_scheduler == []  # a worker is starting
    old = time.time() - worker.LAUNCH_RESERVATION_SECONDS - 1
    os.utime(launch, (old, old))  # its pid may have been reused: the age bound still ends it
    jsonl_store.append_jsonl(runs, {"receipt_id": "r2"})
    assert len(real_learning_scheduler) == 1


def test_a_launched_worker_takes_ownership_and_ends_its_reservation(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    runs.write_text("")
    seen: list[bool] = []
    monkeypatch.setattr(worker, "refresh_snapshot",
                        lambda path: seen.append((tmp_path / worker.LAUNCH).exists()) or "lsnap_x")
    held, release = threading.Event(), threading.Event()

    def holder():
        with jsonl_store.history_file_lock(tmp_path / worker.OWNER):
            held.set()
            release.wait(2)

    t = threading.Thread(target=holder)
    t.start()
    try:
        assert held.wait(2)
        assert worker.update_snapshot(runs) is False  # an unlaunched contender never waits
        (tmp_path / worker.LAUNCH).write_text("tok 1", encoding="ascii")
        threading.Timer(0.05, release.set).start()  # the launcher's section ends quickly
        assert worker.update_snapshot(runs, launch_token="tok") is True
        assert seen == [False]  # the reservation ended as soon as it owned learning
    finally:
        release.set()
        t.join(2)


def test_a_write_during_a_pass_is_drained_before_the_owner_exits(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    jsonl_store.write_jsonl(runs, [osn_entry(repo="shop")])
    (tmp_path / "learning.dirty").write_text("first", encoding="ascii")
    passes: list[int] = []

    def refresh(path):
        passes.append(1)
        if len(passes) == 1:  # a capture lands while the first pass is deriving
            (tmp_path / "learning.dirty").write_text("second", encoding="ascii")
        return "lsnap_x"

    monkeypatch.setattr(worker, "refresh_snapshot", refresh)
    relaunched = Mock()
    monkeypatch.setattr(worker, "_launch_if_idle", relaunched)
    assert worker.update_snapshot(runs) is True
    assert len(passes) == 2 and not relaunched.called


def test_a_write_after_the_last_check_is_handed_to_a_new_worker(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    runs.write_text("")
    dirty = tmp_path / "learning.dirty"
    dirty.write_text("first", encoding="ascii")
    real_stamp, reads = worker._stamp, []

    def stamp(path):
        reads.append(1)
        if len(reads) == 3:  # after the in-lock re-check, before the post-release check
            dirty.write_text("late", encoding="ascii")
        return real_stamp(path)

    monkeypatch.setattr(worker, "_stamp", stamp)
    monkeypatch.setattr(worker, "refresh_snapshot", lambda path: "lsnap_x")
    relaunched = Mock()
    monkeypatch.setattr(worker, "_launch_if_idle", relaunched)
    worker.update_snapshot(runs)
    relaunched.assert_called_once()


def test_a_settling_owner_absorbs_writes_in_its_window_without_a_second_process(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    runs.write_text("")
    dirty = tmp_path / worker.DIRTY
    dirty.write_text("first", encoding="ascii")
    passes: list[int] = []

    def refresh(path):
        passes.append(1)
        if len(passes) == 1:  # a write lands just after the first pass, within the window
            threading.Timer(0.2, lambda: dirty.write_text("second", encoding="ascii")).start()
        return "lsnap_x"

    monkeypatch.setattr(worker, "refresh_snapshot", refresh)
    monkeypatch.setattr(worker, "SETTLE_POLL_SECONDS", 0.02)
    relaunched = Mock()
    monkeypatch.setattr(worker, "_launch_if_idle", relaunched)
    start = time.monotonic()
    assert worker.update_snapshot(runs, settle_seconds=1.0) is True
    assert len(passes) == 2  # the window's write was published by this owner
    assert not relaunched.called  # and needed no second process
    assert time.monotonic() - start >= 1.0  # it then settled once more before exiting


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _streaming_writes(tmp_path, monkeypatch, clock, pass_seconds=1.0):
    """Every pass takes *pass_seconds* and a new write lands during each one."""
    runs = tmp_path / "runs.jsonl"
    runs.write_text("")
    dirty = tmp_path / worker.DIRTY
    passes: list[float] = []

    def refresh(path):
        passes.append(clock.now)
        clock.now += pass_seconds
        dirty.write_text(f"gen{len(passes)}", encoding="ascii")
        return "lsnap_x"

    monkeypatch.setattr(worker, "_clock", clock)
    monkeypatch.setattr(worker, "_sleep", clock.sleep)
    monkeypatch.setattr(worker, "refresh_snapshot", refresh)
    handed_off = Mock()
    monkeypatch.setattr(worker, "_launch_if_idle", handed_off)
    return runs, passes, handed_off


def test_passes_under_a_stream_of_writes_keep_a_minimum_gap(tmp_path, monkeypatch):
    clock = _FakeClock()
    runs, passes, handed_off = _streaming_writes(tmp_path, monkeypatch, clock, pass_seconds=1.0)
    worker.update_snapshot(runs, min_gap_seconds=10.0, max_passes=4)
    assert len(passes) == 4
    assert all(b - a >= 1.0 + 10.0 for a, b in zip(passes, passes[1:]))  # pass + gap, never back to back
    handed_off.assert_called_once()  # the newest write goes to a fresh worker


def test_the_gap_grows_with_a_slow_pass(tmp_path, monkeypatch):
    clock = _FakeClock()
    runs, passes, _ = _streaming_writes(tmp_path, monkeypatch, clock, pass_seconds=30.0)
    worker.update_snapshot(runs, min_gap_seconds=10.0, max_passes=3)
    assert all(b - a >= 30.0 + 2 * 30.0 for a, b in zip(passes, passes[1:]))


def test_no_gap_is_waited_when_no_newer_write_is_pending(tmp_path, monkeypatch):
    clock = _FakeClock()
    runs = tmp_path / "runs.jsonl"
    runs.write_text("")
    monkeypatch.setattr(worker, "_clock", clock)
    monkeypatch.setattr(worker, "_sleep", clock.sleep)
    monkeypatch.setattr(worker, "refresh_snapshot", lambda path: "lsnap_x")
    worker.update_snapshot(runs, min_gap_seconds=10.0, max_lifetime_seconds=600.0)
    assert clock.sleeps == []


def test_a_worker_hands_over_to_a_fresh_one_after_its_lifetime(tmp_path, monkeypatch):
    clock = _FakeClock()
    runs, passes, handed_off = _streaming_writes(tmp_path, monkeypatch, clock, pass_seconds=1.0)
    worker.update_snapshot(runs, min_gap_seconds=10.0, max_lifetime_seconds=60.0)
    assert clock.now <= 60.0 + 1.0  # never past its lifetime by more than one pass
    assert 4 <= len(passes) <= 7
    handed_off.assert_called_once()  # pending write -> fresh process (and fresh code)


def test_the_launched_worker_uses_the_module_bounds(monkeypatch):
    seen = {}
    monkeypatch.setattr(worker, "update_snapshot", lambda runs, **kw: seen.update(kw))
    worker.main(["runs.jsonl", "tok"])
    assert seen["min_gap_seconds"] == worker.MIN_PASS_GAP_SECONDS == 10.0
    assert seen["max_lifetime_seconds"] == worker.MAX_LIFETIME_SECONDS == 600.0
    assert seen["max_passes"] == worker.MAX_PASSES


def test_the_launched_worker_settles_and_in_process_callers_do_not(monkeypatch):
    import inspect

    assert inspect.signature(worker.update_snapshot).parameters["settle_seconds"].default == 0.0
    seen = {}
    monkeypatch.setattr(worker, "update_snapshot", lambda runs, **kw: seen.update(kw))
    worker.main(["runs.jsonl", "tok"])
    assert seen["launch_token"] == "tok" and seen["settle_seconds"] == worker.SETTLE_SECONDS


def test_a_failing_pass_is_retried_a_bounded_number_of_times(tmp_path, monkeypatch):
    runs = tmp_path / "runs.jsonl"
    runs.write_text("")
    (tmp_path / worker.LAUNCH).write_text("tok 1", encoding="ascii")
    monkeypatch.setattr(worker, "PASS_RETRY_DELAYS", (0.0, 0.0, 0.0))
    attempts = Mock(side_effect=PermissionError("snapshot held"))
    monkeypatch.setattr(worker, "refresh_snapshot", attempts)
    relaunched = Mock()
    monkeypatch.setattr(worker, "_launch_if_idle", relaunched)
    with pytest.raises(worker.RefreshFailed):
        worker.update_snapshot(runs, launch_token="tok")
    assert attempts.call_count == 4  # first try + 3 retries, then give up: no loop
    assert not relaunched.called  # no self-relaunch storm
    assert not (tmp_path / worker.LAUNCH).exists()  # nothing blocks the next trigger


def test_after_a_failed_publication_the_next_osn_start_relaunches(tmp_path, real_learning_scheduler, monkeypatch):
    """No new Receipt write is needed: the startup lookup sees the snapshot is behind."""
    repo = _seeded(tmp_path)
    _settled(repo, real_learning_scheduler)
    runs = _runs_path(repo)
    jsonl_store.append_jsonl(runs, osn_entry("Dashboard layout footer", repo="shop"))  # launches (recorded)
    assert len(real_learning_scheduler) == 1
    (repo / ".openshard" / worker.LAUNCH).unlink()  # that worker's publication failed for good
    monkeypatch.setattr(worker, "PASS_RETRY_DELAYS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(snap, "publish_snapshot", Mock(side_effect=PermissionError("held")))
    with pytest.raises(worker.RefreshFailed):
        worker.update_snapshot(runs)
    found = _lookup(repo)
    assert found.status == "available"  # stale but complete is still served
    assert worker.nudge(runs, found) is True and len(real_learning_scheduler) == 2


def _settled(repo, launches) -> None:
    """Forget the launch the seeding write itself made under the real scheduler."""
    launches.clear()
    (repo / ".openshard" / worker.LAUNCH).unlink(missing_ok=True)


def test_nudge_relaunches_for_missing_unusable_or_behind_and_otherwise_stays_quiet(tmp_path, real_learning_scheduler):
    repo = _seeded(tmp_path)
    _settled(repo, real_learning_scheduler)
    runs = _runs_path(repo)
    current = _lookup(repo)
    assert worker.nudge(runs, current) is False  # up to date: one stat, no launch
    for status in ("timeout", "busy", "oversized", "no_history"):
        assert worker.nudge(runs, snap.LearningSnapshot(status, 1.0, 25.0)) is False
    assert real_learning_scheduler == []
    for status in ("missing", "corrupt", "repo_mismatch", "identity_changed"):
        (repo / ".openshard" / worker.LAUNCH).unlink(missing_ok=True)
        assert worker.nudge(runs, snap.LearningSnapshot(status, 1.0, 25.0)) is True
    assert len(real_learning_scheduler) == 4
    real_learning_scheduler.pid = os.getpid()
    with open(runs, "a", encoding="utf-8") as fh:  # history moved on (a worker crashed mid-pass)
        fh.write(json.dumps(osn_entry(repo="shop")) + "\n")
    (repo / ".openshard" / worker.LAUNCH).unlink(missing_ok=True)
    assert worker.nudge(runs, current) is True
    assert worker.nudge(runs, current) is False  # that worker is starting: no second process
    assert len(real_learning_scheduler) == 5
    (repo / ".openshard" / worker.LAUNCH).unlink(missing_ok=True)
    unsourced = snap.LearningSnapshot("available", 1.0, 25.0, index=current.index, source=None)
    assert worker.nudge(runs, unsourced) is False  # cannot be shown to be behind: no launch every run
    assert len(real_learning_scheduler) == 5


def test_the_real_scheduler_starts_a_real_worker_that_publishes(tmp_path, monkeypatch):
    """No stubs: a history write launches a detached interpreter that publishes."""
    monkeypatch.delenv(worker.ENV_SWITCH, raising=False)
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    start = time.perf_counter()
    jsonl_store.write_jsonl(runs, _history())
    assert time.perf_counter() - start < 2  # the writer never waits for derivation
    deadline = time.monotonic() + 60
    found = None
    while time.monotonic() < deadline:
        found = _lookup(repo)
        if found.status == "available" and not (repo / ".openshard" / worker.LAUNCH).exists():
            break
        time.sleep(0.1)
    assert found is not None and found.status == "available" and found.index.receipts_observed == 5
    assert found.source == snap.stat_key(runs)
    deadline = time.monotonic() + 30  # let the worker release its owner lock before tmp cleanup
    while time.monotonic() < deadline:
        try:
            with jsonl_store.history_file_lock(repo / ".openshard" / worker.OWNER, timeout=0):
                break
        except TimeoutError:
            time.sleep(0.05)


# --- One frozen snapshot per OSN run, model-specific context --------------------


def _escalation_history():
    t = "Fix responsive dashboard layout"
    out = []
    for i in range(3):
        out.append(osn_entry(t, attempts=[("fake/a", "passed")], repo="shop", days_ago=0.5 + i / 10))
        out.append(osn_entry(t, attempts=[("fake/b", "passed")], repo="shop", days_ago=0.5 + i / 10))
    return out


@generous
def test_osn_reads_one_snapshot_and_never_history_at_startup(tmp_path, monkeypatch):
    repo = _seeded(tmp_path, _escalation_history())
    lookups = []
    real_lookup = snap.lookup_snapshot
    monkeypatch.setattr(snap, "lookup_snapshot", lambda *a, **kw: lookups.append(1) or real_lookup(*a, **kw))
    forbidden = Mock(side_effect=AssertionError("history read at OSN startup"))
    for target in ("openshard.learning.signals.load_learning_index",
                   "openshard.learning.routing.load_scoped_history",
                   "openshard.routing.adaptive.history_evidence.load_history_evidence"):
        monkeypatch.setattr(target, forbidden)
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a", "--json")
    assert r.exit_code == 0, r.output
    assert len(lookups) == 1
    learning = _runs(repo)[-1]["learning"]
    assert learning["used"] is True and learning["context_supplied"] is True
    assert learning["snapshot"]["status"] == "available"
    assert learning["snapshot"]["snapshot_id"] == _published(repo)["snapshot_id"]


def test_osn_learns_end_to_end_within_the_real_production_budget(tmp_path, monkeypatch):
    """No generous budget: the repository default (25 ms) as shipped."""
    repo = _seeded(tmp_path, _escalation_history())
    outcomes = []
    for _ in range(3):  # a loaded test machine may be late once; it must then say so truthfully
        fp = RecordingProvider([_writes("out.txt", "ok")])
        (repo / "out.txt").write_text("bad")
        r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a", "--json")
        assert r.exit_code == 0, r.output
        learning = _runs(repo)[-1]["learning"]
        assert learning["snapshot"]["budget_ms"] == snap.DEFAULT_BUDGET_MS == 25.0
        outcomes.append(learning["snapshot"]["status"])
        if learning["snapshot"]["status"] == "available":
            assert learning["used"] is True and learning["context_supplied"] is True
            assert learning["snapshot"]["lookup_ms"] < 25.0
            assert "<openshard_history" in fp.calls[0]["prompt"]
            break
        # Late (or an earlier late read still holds the reader slot): said so, with unknown counts.
        assert learning["snapshot"]["status"] in ("timeout", "busy") and learning["status"] == "timeout"
        assert learning["signals_considered"] is None and learning["used"] is False
        if learning["snapshot"]["status"] == "timeout":
            assert learning["snapshot"]["lookup_ms"] >= 25.0
        assert "<openshard_history" not in fp.calls[0]["prompt"]
    # Whether this machine was fast enough is not asserted here (a loaded CI runner may
    # be late every time); that each outcome was truthful is. Speed: the benchmark below.
    assert set(outcomes) <= {"available", "timeout", "busy"}, outcomes


@pytest.mark.skipif(not os.environ.get("OPENSHARD_LEARNING_BENCHMARK"),
                    reason="timing benchmark; set OPENSHARD_LEARNING_BENCHMARK=1 on a quiet machine")
def test_benchmark_osn_learns_within_the_real_production_budget(tmp_path, monkeypatch):
    repo = _seeded(tmp_path, _escalation_history())
    statuses = []
    for _ in range(3):
        fp = RecordingProvider([_writes("out.txt", "ok")])
        (repo / "out.txt").write_text("bad")
        r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a", "--json")
        assert r.exit_code == 0, r.output
        statuses.append(_runs(repo)[-1]["learning"]["snapshot"]["status"])
    assert "available" in statuses, statuses


@generous
def test_each_attempt_sees_only_its_own_models_statistics(tmp_path, monkeypatch):
    repo = _seeded(tmp_path, _escalation_history())
    fp = RecordingProvider([_writes("out.txt", "nope"), _writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a", "--escalate-model", "fake/b", "--json")
    assert r.exit_code == 0, r.output
    first, second = fp.calls[0], fp.calls[1]
    assert first["model"] == "fake/a" and second["model"] == "fake/b"
    found = _lookup(repo)

    def own_summary(model):
        return next(r.signal.summary for r in found.consult(TASK, model=model).retrieved
                    if r.signal.kind == "model_task_outcomes")

    assert found.consult(TASK, model="fake/a").prompt_text in first["prompt"]
    assert found.consult(TASK, model="fake/b").prompt_text in second["prompt"]
    assert own_summary("fake/a") in first["prompt"] and own_summary("fake/b") not in first["prompt"]
    assert own_summary("fake/b") in second["prompt"] and own_summary("fake/a") not in second["prompt"]
    learning = _runs(repo)[-1]["learning"]
    shown = [s.signal_id for m in ("fake/a", "fake/b") for s in
             (r.signal for r in found.consult(TASK, model=m).retrieved)]
    assert learning["signal_ids"] == list(dict.fromkeys(shown))  # every signal that reached a model


@generous
def test_unavailable_learning_starts_the_run_and_says_why(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    jsonl_store.write_jsonl(_runs_path(repo), _history())  # history, never published
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a")
    assert r.exit_code == 0, r.output
    assert "learning snapshot not built yet" in r.output
    entry = _runs(repo)[-1]
    learning = entry["learning"]
    assert learning["status"] == "unavailable" and learning["used"] is False
    assert learning["signals_considered"] is None  # history exists; how much is unknown
    assert learning["snapshot"]["status"] == "missing"
    assert learning_block(entry)["signals_considered"] is None
    assert "<openshard_history" not in fp.calls[0]["prompt"]


def test_osn_start_nudges_a_worker_when_learning_is_missing(tmp_path, monkeypatch, real_learning_scheduler):
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    with open(_runs_path(repo), "w", encoding="utf-8") as fh:  # history written without a worker
        fh.write("".join(json.dumps(e) + "\n" for e in _history()))
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a")
    assert r.exit_code == 0, r.output
    assert len(real_learning_scheduler) == 1  # the run's own Receipt write found that worker starting


def test_no_learning_never_reads_the_snapshot(tmp_path, monkeypatch):
    repo = _seeded(tmp_path)
    monkeypatch.setattr(snap, "lookup_snapshot", Mock(side_effect=AssertionError("--no-learning")))
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, TASK, "--model", "fake/a", "--no-learning", "--json")
    assert r.exit_code == 0, r.output
    learning = _runs(repo)[-1]["learning"]
    assert learning["status"] == "disabled" and "snapshot" not in learning


def test_receipt_projection_adds_snapshot_provenance_only_when_recorded():
    base = {"status": "used", "used": True, "signals_used": 1, "signals_considered": 2, "signal_ids": ["ls_x"]}
    assert "snapshot" not in learning_block({"learning": base})  # older Receipts: unchanged shape
    with_snapshot = learning_block({"learning": {**base, "snapshot": {
        "status": "available", "snapshot_id": "lsnap_1", "generated_at": "2026-10-03T00:00:00Z",
        "lookup_ms": 1.5, "budget_ms": 25,
    }}})
    assert with_snapshot["snapshot"] == {"status": "available", "snapshot_id": "lsnap_1",
                                         "generated_at": "2026-10-03T00:00:00Z", "lookup_ms": 1.5,
                                         "budget_ms": 25.0}
    for status in ("unavailable", "timeout"):
        assert learning_block({"learning": {**base, "status": status, "used": False}})["status"] == status


def test_the_worker_module_runs_detached_and_publishes(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".openshard").mkdir()
    runs = _runs_path(repo)
    jsonl_store.write_jsonl(runs, _history())
    proc = subprocess.Popen([sys.executable, "-m", "openshard.learning.worker", str(runs)])
    assert proc.wait(60) == 0
    assert _lookup(repo).status == "available"
