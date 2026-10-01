"""Failing test ids: observed by OpenShard, stored as identifiers only, learned from, shown as context."""
from __future__ import annotations

import json
import sys

from openshard.learning.retrieval import consult, task_shape_for
from openshard.learning.signals import KIND_TEST, derive_signals, observe
from openshard.osn.loop import run_bounded_loop
from openshard.osn.model_provider import ModelActionProvider
from openshard.verification.failed_tests import MAX_FAILED_TESTS, failing_test_ids
from tests.learning_fixtures import NOW, REPO, osn_entry
from tests.test_learning_osn import RecordingProvider, _invoke, _repo, _runs, _seed, _writes

PY = sys.executable
MOBILE = "tests/test_layout.py::test_mobile_viewport_rejects_non_positive_widths"

PYTEST_OUTPUT = """
F..F                                                                     [100%]
=================================== FAILURES ===================================
E   AssertionError: secret token=abc123 printed here
=========================== short test summary info ============================
FAILED tests/test_layout.py::test_mobile_viewport_rejects_non_positive_widths - Failed: DID NOT RAISE
FAILED tests/test_layout.py::TestCards::test_min_width[sk-abcdefghijklmnopqrstuvwxyz] - assert 1 == 2
ERROR tests/test_db.py::test_connect - OSError
FAILED C:/Users/me/abs/test_x.py::test_abs - boom
FAILED ../outside/test_y.py::test_escape - boom
4 failed, 1 passed in 0.10s
"""


class TestParser:
    def test_pytest_ids_are_kept_and_everything_else_dropped(self):
        assert failing_test_ids(PYTEST_OUTPUT) == [
            MOBILE,
            "tests/test_layout.py::TestCards::test_min_width",  # parametrisation value dropped
            "tests/test_db.py::test_connect",
        ]

    def test_js_test_files(self):
        out = " FAIL  src/dashboard/layout.test.tsx\n  ✕ renders one column\n FAIL src\\ui\\grid.spec.ts > grid"
        assert failing_test_ids(out) == ["src/dashboard/layout.test.tsx", "src/ui/grid.spec.ts"]

    def test_bounded_and_empty(self):
        many = "\n".join(f"FAILED tests/test_a.py::test_{i}" for i in range(20))
        assert len(failing_test_ids(many)) == MAX_FAILED_TESTS
        assert failing_test_ids("") == [] and failing_test_ids(None) == []
        assert failing_test_ids("all good\n3 passed") == []


def _fixture_repo(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "out.txt").write_text("bad")
    (repo / "tests" / "test_out.py").write_text(
        "def test_out_is_ok():\n    assert open('out.txt').read() == 'ok'\n", encoding="utf-8")
    return repo


def test_the_loop_records_failing_ids_for_observed_failures_only(tmp_path):
    repo = _fixture_repo(tmp_path)
    fp = RecordingProvider([_writes("out.txt", "nope"), _writes("out.txt", "ok")])
    ap = ModelActionProvider(fp, ["m1", "m2"], repo)
    receipt = run_bounded_loop(repo, "make out ok", ap, [PY, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                                         "tests/test_out.py"], max_attempts=2)
    assert receipt.status == "verified"
    first, second = receipt.attempts
    assert first.verification.failed_tests == ["tests/test_out.py::test_out_is_ok"]
    assert second.verification.failed_tests == []
    stored = receipt.to_dict()["attempts"][0]["verification"]
    assert stored["failed_tests"] == ["tests/test_out.py::test_out_is_ok"]
    assert "AssertionError" not in json.dumps(receipt.to_dict())  # ids, never output


def test_recurring_test_failure_signal():
    def run(state_first="failed", recovered=True):
        e = osn_entry(attempts=[("m/a", state_first), ("m/b", "passed" if recovered else "failed")])
        e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [MOBILE]
        if not recovered:
            e["osn_loop"]["attempts"][1]["verification"]["failed_tests"] = [MOBILE]
        return e

    idx = derive_signals([run(), run(), run(recovered=False)], repo=REPO, now=NOW)
    [t] = [s for s in idx.signals if s.kind == KIND_TEST]
    assert t.subject == {"test": MOBILE}
    assert t.stats == {"runs_failed": 3, "failed_attempts": 4, "first_attempt_failures": 3, "runs_fixed_later": 2}
    assert "failed OpenShard-run verification in 3 recorded runs" in t.summary
    assert {"mobile", "viewport"} <= set(t.terms) and t.areas[0] == "tests"


def test_stored_ids_are_rechecked_on_read():
    e = osn_entry(attempts=[("m/a", "failed")])
    e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [
        "C:/abs/test_x.py::t", "../x/test_y.py::t", "tests/ok.py::t", 7, "tests/ok.py::t",
    ]
    obs = observe(e)
    assert obs is not None and obs.attempts[0].failed_tests == ("tests/ok.py::t",)


def test_test_name_words_make_a_failing_test_relevant_to_mobile_work():
    entries = []
    for _ in range(2):
        e = osn_entry("Fix responsive dashboard layout", attempts=[("m/a", "failed"), ("m/b", "passed")])
        e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [MOBILE]
        entries.append(e)
    idx = derive_signals(entries, repo=REPO, now=NOW)
    ctx = consult("Make the grid layout work on mobile viewport sizes", idx, repo=REPO)
    tests = [r for r in ctx.retrieved if r.signal.kind == KIND_TEST]
    assert tests and any("mobile" in x and "viewport" in x for x in tests[0].reasons if x.startswith("task_terms:"))
    assert ctx.suggested_context_files == ["tests/test_layout.py"]
    assert task_shape_for("Make the grid layout work on mobile viewport sizes", REPO).task_category == "visual"


def test_osn_run_shows_the_failing_test_file_and_records_it(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_layout.py").write_text("def test_mobile_viewport_rejects_non_positive_widths():\n"
                                                   "    MARKER_FROM_TEST_FILE = 1\n", encoding="utf-8")
    history = []
    for _ in range(2):
        e = osn_entry("Fix responsive dashboard layout", repo="shop",
                      attempts=[("fake/a", "failed"), ("fake/b", "passed")])
        e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [MOBILE]
        history.append(e)
    _seed(repo, history)
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, "Update the dashboard analytics layout", "--model", "fake/b")
    assert r.exit_code == 0, r.output
    assert "+ context tests/test_layout.py" in r.output
    assert '<untrusted file="tests/test_layout.py">' in fp.calls[0]["prompt"]
    assert "MARKER_FROM_TEST_FILE" in fp.calls[0]["prompt"]
    assert _runs(repo)[-1]["learning"]["context_files_added"] == ["tests/test_layout.py"]


def test_missing_or_hidden_suggested_files_are_not_added(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    history = []
    for _ in range(2):
        e = osn_entry("Fix responsive dashboard layout", repo="shop",
                      attempts=[("fake/a", "failed"), ("fake/b", "passed")])
        e["osn_loop"]["attempts"][0]["verification"]["failed_tests"] = [
            "tests/test_gone.py::test_mobile_layout", ".hidden/test_h.py::test_mobile_layout"]
        history.append(e)
    _seed(repo, history)
    fp = RecordingProvider([_writes("out.txt", "ok")])
    r = _invoke(monkeypatch, repo, fp, "Update the dashboard analytics layout", "--model", "fake/b")
    assert r.exit_code == 0, r.output
    assert "+ context" not in r.output and "<untrusted file=" not in fp.calls[0]["prompt"]
    assert "context_files_added" not in _runs(repo)[-1]["learning"]
