"""Unit tests for scripts/bench_claude_capture.py's pure statistics logic.

These test ``Sample``/``_wait_for`` directly -- no service, no subprocess, no
git, nothing that could be mistaken for "running the benchmark". Regression
coverage for a real bug: a fold-wait timeout used to append ``float("nan")``
into a Sample, which silently corrupted that scenario's whole stats block
(NaN sorts unpredictably, and once it enters min/median/p95/p99/max the
result is NaN too) and, once serialized, produced invalid JSON (``NaN``/
``Infinity`` are not valid JSON tokens per RFC 8259, though Python's ``json``
module emits them unless told not to).
"""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "bench_claude_capture.py"
_spec = importlib.util.spec_from_file_location("bench_claude_capture", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = bench
_spec.loader.exec_module(bench)

Sample = bench.Sample
_wait_for = bench._wait_for


class TestWaitForNeverReturnsNaN:
    def test_success_returns_a_float(self):
        result = _wait_for(lambda: True, timeout=1.0)
        assert isinstance(result, float)
        assert math.isfinite(result)

    def test_timeout_returns_none_not_nan(self):
        result = _wait_for(lambda: False, timeout=0.05)
        assert result is None


class TestSampleStatsNeverProducesNaNOrInfinity:
    def test_all_valid_samples_computes_normal_stats(self):
        s = Sample("x", seconds=[0.01, 0.02, 0.03, 0.04, 0.05])
        st = s.stats()
        assert st["n"] == 5
        assert st["attempted"] == 5
        assert st["partial"] is False
        assert "timed_out" not in st
        for key in ("min_ms", "median_ms", "p95_ms", "p99_ms", "max_ms"):
            assert isinstance(st[key], float)
            assert math.isfinite(st[key])
        assert st["min_ms"] == pytest.approx(10.0)
        assert st["max_ms"] == pytest.approx(50.0)

    def test_a_timed_out_entry_is_excluded_not_nan(self):
        # Exactly the shape a fold-wait timeout used to produce: a real
        # completed sample followed by one that never finished in time.
        s = Sample("service: Stop POST returned -> receipt folded in runs.jsonl",
                    seconds=[0.02, 0.03, None, 0.04])
        st = s.stats()
        assert st["n"] == 3  # only the completed ones
        assert st["attempted"] == 4
        assert st["timed_out"] == 1
        assert st["partial"] is True
        for key in ("min_ms", "median_ms", "p95_ms", "p99_ms", "max_ms"):
            assert isinstance(st[key], float)
            assert math.isfinite(st[key])

    def test_every_attempt_timed_out_yields_null_stats_not_nan(self):
        s = Sample("x", seconds=[None, None, None])
        st = s.stats()
        assert st["n"] == 0
        assert st["attempted"] == 3
        assert st["timed_out"] == 3
        assert st["partial"] is True
        for key in ("min_ms", "median_ms", "p95_ms", "p99_ms", "max_ms"):
            assert st[key] is None

    def test_nothing_attempted_yields_null_stats_and_is_not_partial(self):
        s = Sample("x", seconds=[])
        st = s.stats()
        assert st == {
            "n": 0, "attempted": 0, "partial": False,
            "min_ms": None, "median_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None,
        }

    def test_budget_exceeded_marks_partial_even_when_every_sample_is_valid(self):
        s = Sample("x", seconds=[0.01, 0.02], partial=True)
        st = s.stats()
        assert st["n"] == 2
        assert st["attempted"] == 2
        assert "timed_out" not in st
        assert st["partial"] is True

    def test_stats_output_is_strict_json_serializable(self):
        for seconds in ([0.01, 0.02, None], [None, None], [], [0.001] * 50):
            st = Sample("x", seconds=seconds).stats()
            blob = json.dumps(st, allow_nan=False)  # raises on any NaN/Infinity
            restored = json.loads(blob)
            assert restored == st

    def test_stray_nan_or_infinity_would_be_caught_by_allow_nan_false(self):
        # Sanity-check the defense-in-depth guard itself: if a NaN or
        # Infinity ever did end up in a stats dict again, allow_nan=False
        # must reject it rather than silently emitting invalid JSON.
        with pytest.raises(ValueError):
            json.dumps({"p99_ms": float("nan")}, allow_nan=False)
        with pytest.raises(ValueError):
            json.dumps({"max_ms": float("inf")}, allow_nan=False)
