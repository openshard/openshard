"""Code health ratchet for the ``openshard`` package.

Four maintainability signals are measured by ``scripts/code_health.py``
(standard library only, so CI and every machine agree): module length,
function length, cyclomatic complexity and the number of broad
``except Exception`` handlers per module. Each has a general ceiling that
every new or small unit must respect, and a budget table for the units that
already exceed it, recorded at their size on the day the ratchet was added.

The tables work like ``KNOWN_EXCEPTIONS`` in ``test_architecture_boundaries``:
growing a unit past its budget fails, a unit that shrank well below its
budget fails until the budget is lowered, and a unit that drops under the
general ceiling must leave the table. ``python scripts/code_health.py
--budgets`` prints the literals to paste, so a budget is always a copied
measurement. Raising a budget needs a reason a reviewer can accept; the
intended way to add a command, a branch or a handler to a listed unit is to
extract something from it first.

Why these ceilings: 1500 lines keeps a module readable in one sitting; a
150-line function still fits a screen and a head; cyclomatic complexity 25
is where a function stops being testable path by path; five broad handlers
per module is enough for the deliberate "observation never breaks the
agent" guards the capture path needs and too few to hide defects.
"""

from __future__ import annotations

import importlib.util
import sys
from collections import defaultdict
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "code_health.py"

MODULE_LINE_CEILING = 1500
FUNCTION_LINE_CEILING = 150
COMPLEXITY_CEILING = 25
BROAD_EXCEPT_CEILING = 5

# Units already over a ceiling, at the size measured when the ratchet landed
# (main at fd70787a, 2026-10-12). Capture service and hook module budgets include the capture-root repair (#528)
# and the queued start-HEAD fix (#527): 1642 and 4945 lines respectively.
# Shrink the numbers; delete an entry once its
# unit is under the ceiling.
MODULE_LINE_BUDGETS = {
    # +7: `setup --no-commit-trailers` and the setup/doctor lines for agent commit trailers;
    # the logic lives in adapters/agent_commit_trailer.py.
    "openshard/cli/main.py": 8461,
    "openshard/adapters/claude_hooks.py": 4524,
    "openshard/native/context.py": 3742,
    # +3: the compact Receipt says agent-reported files are also in git's diff (ShardReceipt.agent_reported_in_git).
    "openshard/history/shard_contract.py": 1810,
    "openshard/run/pipeline.py": 2827,
    "openshard/cli/run_output.py": 2580,
    "openshard/cli/osn_cmd.py": 2388,
    "openshard/native/executor.py": 1683,
    "openshard/adapters/claude_capture_service.py": 1637,
}

FUNCTION_LINE_BUDGETS = {
    ("openshard/run/pipeline.py", "RunPipeline.run"): 2473,
    ("openshard/cli/run_output.py", "_render_native_demo_block"): 1046,
    ("openshard/osn/loop.py", "run_bounded_loop"): 681,
    ("openshard/history/shard_contract.py", "build_shard_receipt"): 606,
    ("openshard/cli/osn_cmd.py", "osn_run"): 579,
    ("openshard/history/receipt_render.py", "render_full_shard_receipt"): 496,
    ("openshard/cli/main.py", "_render_log_entry"): 433,
    ("openshard/adapters/claude_hooks.py", "build_hook_entry"): 398,
    ("openshard/adapters/claude_hooks.py", "_apply"): 325,
    ("openshard/run/_pipeline_helpers.py", "_log_run"): 343,
    ("openshard/osn/agent_loop.py", "run_attempt_turns"): 334,
    ("openshard/native/context.py", "build_native_model_candidate_scoring"): 259,
    ("openshard/mcp/server.py", "build_server"): 245,
    ("openshard/osn/run_entry.py", "build_osn_run_entry"): 234,
    ("openshard/native/context.py", "build_native_context_provenance"): 218,
    ("openshard/native/executor.py", "NativeAgentExecutor.generate"): 212,
    ("openshard/cli/main.py", "doctor"): 198,
    ("openshard/cli/osn_cmd.py", "_resolve_workers"): 191,
    ("openshard/adapters/claude_capture_service.py", "serve"): 177,
    ("openshard/adapters/grok_bot.py", "build_report_entry"): 175,
    ("openshard/cli/osn_cmd.py", "_OsnProgressRenderer._render"): 172,
    ("openshard/insights/warehouse.py", "ReceiptWarehouse._load"): 169,
    ("openshard/review/checks.py", "run_review_checks"): 169,
    ("openshard/adapters/claude_hooks.py", "_buffer_from_entry"): 165,
    ("openshard/insights/query.py", "answer_question"): 159,
    ("openshard/remote/collector.py", "flush"): 159,
    ("openshard/history/routing_truth.py", "build_routing_truth"): 156,
    ("openshard/history/verification_truth.py", "interpret_evidence"): 153,
    ("openshard/adapters/claude_setup.py", "run_setup"): 152,
}

COMPLEXITY_BUDGETS = {
    ("openshard/run/pipeline.py", "RunPipeline.run"): 482,
    ("openshard/cli/run_output.py", "_render_native_demo_block"): 300,
    ("openshard/history/receipt_render.py", "render_full_shard_receipt"): 170,
    ("openshard/history/shard_contract.py", "build_shard_receipt"): 136,
    ("openshard/cli/osn_cmd.py", "osn_run"): 121,
    ("openshard/cli/main.py", "_render_log_entry"): 110,
    ("openshard/adapters/claude_hooks.py", "_apply"): 81,
    ("openshard/native/context.py", "build_native_model_candidate_scoring"): 93,
    ("openshard/adapters/claude_hooks.py", "build_hook_entry"): 84,
    ("openshard/osn/loop.py", "run_bounded_loop"): 82,
    ("openshard/cli/osn_cmd.py", "_OsnProgressRenderer._render"): 67,
    ("openshard/run/_pipeline_helpers.py", "_log_run"): 60,
    ("openshard/osn/run_entry.py", "build_osn_run_entry"): 55,
    ("openshard/osn/agents.py", "build_agent_graph"): 54,
    ("openshard/osn/agent_loop.py", "run_attempt_turns"): 52,
    ("openshard/history/receipt_render.py", "_render_osn_compact_receipt"): 49,
    ("openshard/native/context.py", "build_native_context_provenance"): 48,
    ("openshard/history/receipt_render.py", "_render_osn_parallel"): 44,
    ("openshard/cli/main.py", "doctor"): 41,
    ("openshard/adapters/claude_hooks.py", "_buffer_from_entry"): 39,
    ("openshard/native/progress_memory.py", "build_osn_progress_memory"): 38,
    ("openshard/history/verification_truth.py", "interpret_evidence"): 37,
    ("openshard/cli/main.py", "report"): 36,
    ("openshard/cli/run_output.py", "render_post_run"): 35,
    ("openshard/remote/collector.py", "flush"): 34,
    ("openshard/adapters/cursor_usage.py", "events_usage_block"): 32,
    ("openshard/history/receipt_render.py", "render_compact_shard_receipt"): 32,
    ("openshard/insights/query.py", "answer_question"): 32,
    ("openshard/tui/commands.py", "parse_tui_input"): 30,
    ("openshard/verification/plan.py", "classify_command_safety"): 30,
    ("openshard/adapters/grok_bot.py", "build_report_entry"): 29,
    ("openshard/cli/main.py", "eval_compare"): 29,
    ("openshard/cli/main.py", "eval_stats"): 29,
    ("openshard/cli/run_output.py", "_render_tier_dispatch_block"): 29,
    ("openshard/models/advisory.py", "recommend_models"): 29,
    ("openshard/native/context.py", "build_native_run_trust_score"): 29,
    ("openshard/osn/model_provider.py", "build_turn_prompt"): 29,
    ("openshard/cli/main.py", "remote_status"): 28,
    ("openshard/cli/osn_cmd.py", "_resolve_workers.workers_hook"): 28,
    ("openshard/osn/roles.py", "RoleRun.from_usage"): 28,
    ("openshard/sync/client.py", "_flush_usage"): 28,
    ("openshard/evals/receipt_honesty.py", "check_rules"): 27,
    ("openshard/cli/main.py", "eval_report"): 26,
    ("openshard/learning/snapshot.py", "_read"): 26,
}

BROAD_EXCEPT_BUDGETS = {
    "openshard/adapters/claude_hooks.py": 26,
    "openshard/run/pipeline.py": 27,
    "openshard/adapters/claude_capture_client.py": 19,
    "openshard/run/_pipeline_helpers.py": 17,
    "openshard/cli/main.py": 15,
    "openshard/history/event.py": 15,
    "openshard/sync/client.py": 13,
    "openshard/adapters/claude_capture_service.py": 11,
    "openshard/adapters/claude_hooks_install.py": 10,
    "openshard/scoring/filter.py": 8,
    "openshard/cli/osn_cmd.py": 7,
    "openshard/config/settings.py": 7,
    "openshard/telemetry/client.py": 7,
    "openshard/adapters/claude_code_import.py": 6,
    "openshard/cli/ui/home.py": 6,
    "openshard/remote/collector.py": 6,
    "openshard/sync/transport.py": 6,
    "openshard/telemetry/queue.py": 6,
}


def _load():
    spec = importlib.util.spec_from_file_location("code_health", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def health():
    return _load()


@pytest.fixture(scope="module")
def functions(health) -> dict[tuple[str, str], tuple[int, int]]:
    """(module, qualname) -> (length, complexity); a repeated name keeps its largest values."""
    out: dict[tuple[str, str], tuple[int, int]] = defaultdict(lambda: (0, 0))
    for f in health.function_measures(REPO_ROOT):
        key = (f.module, f.qualname)
        length, complexity = out[key]
        out[key] = (max(length, f.length), max(complexity, f.complexity))
    return dict(out)


def _fail(title: str, lines: list[str]) -> None:
    if lines:
        pytest.fail(title + "\n  " + "\n  ".join(sorted(lines)))


# ---------------------------------------------------------------------------
# Nothing grows past its budget
# ---------------------------------------------------------------------------


def test_no_module_grows_past_its_budget(health) -> None:
    over = []
    for module, lines in health.module_lines(REPO_ROOT).items():
        budget = MODULE_LINE_BUDGETS.get(module, MODULE_LINE_CEILING)
        if lines > budget:
            over.append(f"{module}: {lines} lines > {budget}")
    _fail(
        "Modules grew past their budget. Extract a cohesive piece into its own module "
        "before adding to these (python scripts/code_health.py prints the report):",
        over,
    )


def test_no_function_grows_past_its_budget(functions) -> None:
    over = []
    for (module, name), (length, _) in functions.items():
        budget = FUNCTION_LINE_BUDGETS.get((module, name), FUNCTION_LINE_CEILING)
        if length > budget:
            over.append(f"{module}::{name}: {length} lines > {budget}")
    _fail("Functions grew past their line budget. Split them before adding to them:", over)


def test_no_function_grows_more_complex_than_its_budget(functions) -> None:
    over = []
    for (module, name), (_, complexity) in functions.items():
        budget = COMPLEXITY_BUDGETS.get((module, name), COMPLEXITY_CEILING)
        if complexity > budget:
            over.append(f"{module}::{name}: complexity {complexity} > {budget}")
    _fail("Functions grew more complex than their budget. Move a branch into a helper:", over)


def test_no_module_adds_broad_except_handlers(health) -> None:
    over = []
    for module, count in health.broad_excepts(REPO_ROOT).items():
        budget = BROAD_EXCEPT_BUDGETS.get(module, BROAD_EXCEPT_CEILING)
        if count > budget:
            over.append(f"{module}: {count} broad handlers > {budget}")
    _fail(
        "Modules gained broad `except Exception` handlers. Catch the failure you expect "
        "(OSError, ValueError, LockTimeoutError, ...) or record why observation must never raise:",
        over,
    )


# ---------------------------------------------------------------------------
# Every budget is current: it names a real unit, still over the ceiling, and
# is not far above what the unit measures today.
# ---------------------------------------------------------------------------


def _stale(label: str, budget: int, actual: int | None, ceiling: int, slack: int) -> str | None:
    if actual is None:
        return f"{label}: no longer exists; delete its budget"
    if actual <= ceiling:
        return f"{label}: now {actual}, under the ceiling {ceiling}; delete its budget"
    if actual < budget - slack:
        return f"{label}: now {actual}, budget {budget}; lower the budget"
    return None


def test_module_budgets_are_current(health) -> None:
    lines = health.module_lines(REPO_ROOT)
    stale = []
    for module, budget in MODULE_LINE_BUDGETS.items():
        problem = _stale(module, budget, lines.get(module), MODULE_LINE_CEILING, max(50, budget // 10))
        if problem:
            stale.append(problem)
    _fail("Module budgets are stale:", stale)


def test_function_budgets_are_current(functions) -> None:
    stale = []
    for (module, name), budget in FUNCTION_LINE_BUDGETS.items():
        measured = functions.get((module, name))
        problem = _stale(
            f"{module}::{name}", budget, measured[0] if measured else None, FUNCTION_LINE_CEILING, max(10, budget // 10)
        )
        if problem:
            stale.append(problem)
    _fail("Function line budgets are stale:", stale)


def test_complexity_budgets_are_current(functions) -> None:
    stale = []
    for (module, name), budget in COMPLEXITY_BUDGETS.items():
        measured = functions.get((module, name))
        problem = _stale(
            f"{module}::{name}", budget, measured[1] if measured else None, COMPLEXITY_CEILING, max(3, budget // 10)
        )
        if problem:
            stale.append(problem)
    _fail("Complexity budgets are stale:", stale)


def test_broad_except_budgets_are_current(health) -> None:
    counts = health.broad_excepts(REPO_ROOT)
    stale = []
    for module, budget in BROAD_EXCEPT_BUDGETS.items():
        problem = _stale(module, budget, counts.get(module), BROAD_EXCEPT_CEILING, 0)
        if problem:
            stale.append(problem)
    _fail("Broad-except budgets are stale:", stale)


# ---------------------------------------------------------------------------
# The measurements themselves
# ---------------------------------------------------------------------------


def test_measures_count_what_they_claim(health, tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "a.py").write_text(
        "def f(x):\n"
        "    if x:\n"
        "        for i in x:\n"
        "            pass\n"
        "    try:\n"
        "        pass\n"
        "    except Exception:\n"
        "        pass\n"
        "    except (ValueError, BaseException):\n"
        "        pass\n"
        "    except OSError:\n"
        "        pass\n"
        "    return [y for y in x if y]\n"
        "\n"
        "class C:\n"
        "    def m(self):\n"
        "        def inner():\n"
        "            if self:\n"
        "                pass\n"
        "        return inner\n",
        encoding="utf-8",
    )
    measures = {(m.module, m.qualname): m for m in health.function_measures(tmp_path, "pkg")}
    f = measures[("pkg/a.py", "f")]
    # 1 + if + for + 3 except handlers + 1 comprehension generator = 7 (its filter is an expression)
    assert (f.length, f.complexity) == (13, 7)
    assert measures[("pkg/a.py", "C.m")].complexity == 1  # the nested function is counted on its own
    assert measures[("pkg/a.py", "C.m.inner")].complexity == 2
    assert health.broad_excepts(tmp_path, "pkg") == {"pkg/a.py": 2}
    assert health.module_lines(tmp_path, "pkg") == {"pkg/__init__.py": 0, "pkg/a.py": 20}
