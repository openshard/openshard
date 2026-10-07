"""The OSN acceptance runner summarises runs truthfully without running any model."""
from __future__ import annotations

import json
from pathlib import Path

from evals.osn_acceptance.run_acceptance import (
    DEFAULT_TASKS,
    check_expectation,
    load_tasks,
    osn_run_command,
    render_table,
    summarise,
)


def _result(**over):
    base = {
        "status": "verified", "verification_state": "passed", "attempts": 1, "turns": 5,
        "changed_files": ["title.py", "tests/test_title.py"], "receipt_id": "rcpt_x",
        "action_summary": {"actions": 9, "writes_applied": 2},
        "model_calls": [{"cost_usd": 0.01, "cost_source": "provider_reported"},
                        {"cost_usd": 0.0025, "cost_source": "provider_reported"}],
    }
    base.update(over)
    return base


def test_the_shipped_tasks_load_and_build_commands():
    tasks = load_tasks(DEFAULT_TASKS)
    assert [t["id"] for t in tasks] == ["feature-title-slug", "multi-file-stats", "refactor-join-type-hints",
                                        "policy-blocked-secret"]
    argv = osn_run_command(tasks[0])
    assert argv[3:6] == ["osn", "run", tasks[0]["task"]] and "--json" in argv and "--roles" in argv


def test_expectations_are_checked_against_the_result_object():
    assert check_expectation({"status": "verified", "max_attempts": 1}, _result()) == (True, "met")
    assert check_expectation({"status": "verified"}, _result(status="failed"))[0] is False
    assert check_expectation({"changed_at_least": 3}, _result()) == (False, "changed 2 < 3")
    assert check_expectation({"writes_applied": 0}, _result(status="blocked", action_summary={"writes_applied": 0}))[0]
    assert check_expectation({"status": "blocked"}, None) == (False, "no result object")
    assert check_expectation(None, None) == (True, "no expectation")


def test_summary_keeps_cost_unknown_when_any_call_has_none_and_renders_a_table():
    rows = [
        {"task": {"id": "a", "kind": "feature", "expect": {"status": "verified"}}, "exit_code": 0, "wall_seconds": 12.3,
         "result": _result()},
        {"task": {"id": "b", "kind": "policy", "expect": {"status": "blocked"}}, "exit_code": 0, "wall_seconds": 3.0,
         "result": _result(status="failed", model_calls=[{"cost_usd": None, "cost_source": None}])},
        {"task": {"id": "c", "kind": "x"}, "exit_code": 2, "wall_seconds": 0.5, "result": None},
    ]
    summary = summarise(rows)
    assert summary[0]["cost_usd"] == 0.0125 and summary[0]["cost_source"] == "provider_reported"
    assert summary[0]["expectation"] == "met"
    assert summary[1]["cost_usd"] is None and summary[1]["cost_source"] == "unknown"
    assert summary[1]["expectation"] == "NOT met: status 'failed' != 'blocked'"
    assert summary[2]["status"] == "no result (exit 2)" and summary[2]["expectation"] == "no expectation"
    table = render_table(summary)
    assert table.splitlines()[0].startswith("| task | status |")
    assert "| a | verified | passed | 1 | 5 | 9 | 2 | $0.0125 (provider_reported) | 12.3 | met |" in table
    assert "unknown" in table.splitlines()[3]
    assert json.loads(json.dumps(summary)) == summary  # JSON-safe
    assert Path(DEFAULT_TASKS).name == "tasks.json"
