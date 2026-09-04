"""Machine-readable run results and the human-readable A/B comparison.

One ``RunResult`` per agent run (burn-in, control arm, treatment arm).
Every field is either observed or ``None``/``"unknown"``; nothing is
estimated. Token and cost figures come only from Claude Code's own
``result`` event and are labelled with that provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.pr13.benchmark.harness import HARNESS_NAME, OPENSHARD_TOOL_PREFIX, AgentRun

RESULT_SCHEMA_VERSION = 1
ARM_CONTROL = "A"
ARM_TREATMENT = "B"
ARM_BURN_IN = "burn_in"
ARM_LABELS = {ARM_CONTROL: "control (placebo OpenShard MCP, empty history)",
              ARM_TREATMENT: "treatment (production OpenShard MCP, preserved history)",
              ARM_BURN_IN: "burn-in (captured prior attempt)"}


def retrieval_observed(run: AgentRun | None, *, mcp_configured: bool) -> str:
    """``yes`` / ``no`` / ``unknown``: did the agent call an OpenShard MCP tool?"""
    if run is None or run.launch_error:
        return "unknown"
    stream = run.parsed
    if stream.lines_total == 0 or (stream.lines_unparsed and not stream.session_id):
        return "unknown"
    if any(c.name.startswith(OPENSHARD_TOOL_PREFIX) for c in stream.tool_calls):
        return "yes"
    if not mcp_configured:
        return "no"
    # Treatment: the server had to be connected for "no" to mean the agent chose not to call it.
    statuses = {s.get("name"): s.get("status") for s in stream.mcp_servers}
    if statuses.get("openshard") == "connected":
        return "no"
    return "unknown"


@dataclass
class RunResult:
    scenario: str
    arm: str
    repeat: int
    base_commit: str
    workspace: str
    run_dir: str
    harness: dict[str, Any]
    model_requested: str
    model_reported_init: str | None
    models_observed: list[str]
    started_at: str
    ended_at: str
    wall_clock_seconds: float
    agent_exit_status: str
    agent_exit_code: int | None
    agent_timed_out: bool
    agent_reported_completion: bool | None
    agent_result_subtype: str | None
    agent_num_turns: int | None
    agent_final_text: str | None
    activity: dict[str, Any]
    verification: dict[str, Any]
    repeated_known_failure: dict[str, Any]
    openshard: dict[str, Any]
    usage: dict[str, Any]
    artifacts: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def verified_success(self) -> bool | None:
        value = self.verification.get("passed")
        return bool(value) if isinstance(value, bool) else None

    @property
    def repeated_known_failure_matched(self) -> bool | None:
        value = self.repeated_known_failure.get("matched")
        return bool(value) if isinstance(value, bool) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESULT_SCHEMA_VERSION,
            "benchmark": "pr13",
            "scenario": self.scenario,
            "arm": self.arm,
            "arm_label": ARM_LABELS.get(self.arm, self.arm),
            "repeat": self.repeat,
            "base_commit": self.base_commit,
            "workspace": self.workspace,
            "run_dir": self.run_dir,
            "agent": {"harness": HARNESS_NAME, **self.harness},
            "model": {
                "requested": self.model_requested,
                "reported_by_claude_init": self.model_reported_init,
                "observed_in_assistant_messages": list(self.models_observed),
            },
            "timing": {
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "wall_clock_seconds": self.wall_clock_seconds,
            },
            "agent_exit": {
                "status": self.agent_exit_status,
                "exit_code": self.agent_exit_code,
                "timed_out": self.agent_timed_out,
                "result_subtype": self.agent_result_subtype,
                "agent_reported_completion": self.agent_reported_completion,
                "num_turns": self.agent_num_turns,
                "final_text": self.agent_final_text,
            },
            "activity": self.activity,
            "verification": self.verification,
            "verified_success": self.verified_success,
            "repeated_known_failure": self.repeated_known_failure,
            "openshard": self.openshard,
            "usage": self.usage,
            "artifacts": self.artifacts,
            "errors": list(self.errors),
            "notes": list(self.notes),
        }

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str) + "\n", encoding="utf-8")


def usage_from_run(run: AgentRun | None) -> dict[str, Any]:
    """Cost/token figures as Claude Code reported them, or explicit unknowns."""
    if run is None or not run.parsed.saw_result:
        return {"total_cost_usd": None, "cost_provenance": None, "tokens": None,
                "tokens_provenance": None, "model_usage": None, "trustworthy": False,
                "note": "no result event from Claude Code; usage unknown"}
    stream = run.parsed
    tokens = None
    if isinstance(stream.usage, dict):
        tokens = {
            "input": stream.usage.get("input_tokens"),
            "output": stream.usage.get("output_tokens"),
            "cache_read": stream.usage.get("cache_read_input_tokens"),
            "cache_creation": stream.usage.get("cache_creation_input_tokens"),
        }
    return {
        "total_cost_usd": stream.total_cost_usd,
        "cost_provenance": "claude_code_result_event" if stream.total_cost_usd is not None else None,
        "tokens": tokens,
        "tokens_provenance": "claude_code_result_event" if tokens else None,
        "model_usage": stream.model_usage,
        "trustworthy": stream.total_cost_usd is not None or tokens is not None,
        "note": "Claude Code's own accounting for this session; approximate, not billing truth",
    }


def activity_from_run(run: AgentRun | None, changed: dict[str, Any]) -> dict[str, Any]:
    if run is None:
        return {"tool_calls_total": None, "tool_calls_by_name": {}, "bash_commands": [], "files_changed": changed,
                "trustworthy": False}
    stream = run.parsed
    return {
        "tool_calls_total": len(stream.tool_calls),
        "tool_calls_by_name": stream.tool_counts(),
        "bash_commands": [c.input_summary for c in stream.tool_calls if c.name == "Bash" and c.input_summary],
        "files_changed": changed,
        "trustworthy": stream.saw_result and stream.lines_unparsed == 0,
        "source": "claude_code_stream_json",
    }


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _fmt(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}" if value < 10 else f"{value:.1f}"
    return str(value)


def build_comparison(burn_in: dict[str, Any] | None, arms: list[dict[str, Any]], *, options: dict[str, Any]) -> dict[str, Any]:
    """Structured A/B comparison over all repeats. No causal claim is made or implied."""
    by_arm: dict[str, list[dict[str, Any]]] = {}
    for r in arms:
        by_arm.setdefault(r["arm"], []).append(r)

    def summarise(runs: list[dict[str, Any]]) -> dict[str, Any]:
        def count(pred: Any) -> int:
            return sum(1 for r in runs if pred(r))

        return {
            "runs": len(runs),
            "verified_success": count(lambda r: r.get("verified_success") is True),
            "verified_failure": count(lambda r: r.get("verified_success") is False),
            "verification_unknown": count(lambda r: r.get("verified_success") is None),
            "repeated_known_failure": count(lambda r: r["repeated_known_failure"].get("matched") is True),
            "agent_reported_completion": count(lambda r: r["agent_exit"].get("agent_reported_completion") is True),
            "timeouts": count(lambda r: r["agent_exit"].get("timed_out")),
            "openshard_retrieval_yes": count(lambda r: r["openshard"].get("retrieval_observed") == "yes"),
            "openshard_retrieval_unknown": count(lambda r: r["openshard"].get("retrieval_observed") == "unknown"),
            "mcp_server_kinds": sorted({str(r["openshard"].get("mcp_server_kind")) for r in runs}),
            "mcp_surface_fingerprints": sorted({str((r["openshard"].get("mcp_surface") or {}).get("fingerprint")) for r in runs}),
            "wall_clock_seconds": [r["timing"]["wall_clock_seconds"] for r in runs],
            "num_turns": [r["agent_exit"].get("num_turns") for r in runs],
            "tool_calls_total": [r["activity"].get("tool_calls_total") for r in runs],
            "total_cost_usd": [r["usage"].get("total_cost_usd") for r in runs],
            "models_observed": sorted({m for r in runs for m in r["model"]["observed_in_assistant_messages"]}),
        }

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "question": "Same repository state, prompt, model and tool surface. Control receives empty OpenShard "
                    "history (placebo MCP server); treatment receives real OpenShard execution evidence "
                    "(production MCP server over the preserved burn-in history). Does the verified outcome change?",
        "options": options,
        "burn_in": burn_in,
        "control": summarise(by_arm.get(ARM_CONTROL, [])),
        "treatment": summarise(by_arm.get(ARM_TREATMENT, [])),
        "paired_runs": [
            {
                "repeat": rep,
                "control": next((r["run_dir"] for r in by_arm.get(ARM_CONTROL, []) if r["repeat"] == rep), None),
                "treatment": next((r["run_dir"] for r in by_arm.get(ARM_TREATMENT, []) if r["repeat"] == rep), None),
            }
            for rep in sorted({r["repeat"] for r in arms})
        ],
        "caveats": [
            "One paired run (or a handful) cannot establish causality; treat differences as observations to replicate.",
            "The only intended difference between arms is the OpenShard history behind an identical MCP tool "
            "surface (control: placebo server, empty answers; treatment: production server, preserved burn-in "
            "history); everything else is identical by construction, but model sampling is not deterministic.",
            "Hook-captured burn-in history records no verification outcome; the treatment agent sees a prior "
            "attempt's task, files and activity, not a failure verdict.",
        ],
    }


def render_comparison_markdown(comparison: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    c, t = comparison["control"], comparison["treatment"]
    lines = [
        "# PR13 Phase 1 -- OpenShard effectiveness benchmark",
        "",
        f"**Scenario:** {comparison['options'].get('scenario')}  ",
        f"**Model requested:** {comparison['options'].get('model')}  ",
        f"**Base commit:** `{comparison['options'].get('base_commit')}`  ",
        f"**Arm order:** {comparison['options'].get('arm_order')} · **Repeats:** {comparison['options'].get('repeats')}",
        "",
        "## Question",
        "",
        comparison["question"],
        "",
        "## Burn-in (captured prior attempt)",
        "",
    ]
    b = comparison.get("burn_in") or {}
    if b:
        lines += [
            f"- Verification failed: **{_fmt(b.get('verification_failed'))}**",
            f"- Known failed approach matched: **{_fmt(b.get('known_failed_approach_matched'))}**",
            f"- Expected evidence present in preserved history: **{_fmt(b.get('expected_evidence_present'))}**",
            f"- Burn-in Shard retrievable via `relevant_context(evaluation prompt)`: **{_fmt(b.get('retrievable'))}**",
            f"- Shards preserved: {_fmt(b.get('shards'))}",
        ]
    else:
        lines.append("- not run")
    lines += ["", "## Arms", "",
              "| Metric | Control A (placebo OpenShard MCP, empty history) | Treatment B (production OpenShard MCP, preserved history) |",
              "|---|---|---|"]
    rows = [
        ("Runs", c["runs"], t["runs"]),
        ("Verified success", c["verified_success"], t["verified_success"]),
        ("Verified failure", c["verified_failure"], t["verified_failure"]),
        ("Verification unknown", c["verification_unknown"], t["verification_unknown"]),
        ("Repeated known failed approach", c["repeated_known_failure"], t["repeated_known_failure"]),
        ("Agent reported completion", c["agent_reported_completion"], t["agent_reported_completion"]),
        ("Timeouts", c["timeouts"], t["timeouts"]),
        ("OpenShard retrieval observed (yes)", c["openshard_retrieval_yes"], t["openshard_retrieval_yes"]),
        ("OpenShard retrieval unknown", c["openshard_retrieval_unknown"], t["openshard_retrieval_unknown"]),
        ("MCP server kind", ", ".join(c["mcp_server_kinds"]), ", ".join(t["mcp_server_kinds"])),
        ("MCP tool-surface fingerprint", ", ".join(f[:12] for f in c["mcp_surface_fingerprints"]),
         ", ".join(f[:12] for f in t["mcp_surface_fingerprints"])),
        ("Wall-clock seconds (per run)", ", ".join(_fmt(v) for v in c["wall_clock_seconds"]), ", ".join(_fmt(v) for v in t["wall_clock_seconds"])),
        ("Turns (per run)", ", ".join(_fmt(v) for v in c["num_turns"]), ", ".join(_fmt(v) for v in t["num_turns"])),
        ("Tool calls (per run)", ", ".join(_fmt(v) for v in c["tool_calls_total"]), ", ".join(_fmt(v) for v in t["tool_calls_total"])),
        ("Cost USD, Claude-reported (per run)", ", ".join(_fmt(v) for v in c["total_cost_usd"]), ", ".join(_fmt(v) for v in t["total_cost_usd"])),
        ("Models observed", ", ".join(c["models_observed"]) or "unknown", ", ".join(t["models_observed"]) or "unknown"),
    ]
    for label, a, bb in rows:
        lines.append(f"| {label} | {_fmt(a)} | {_fmt(bb)} |")
    lines += ["", "## Per-run detail", ""]
    for r in runs:
        lines += [
            f"### {r['arm_label']} · repeat {r['repeat']}",
            "",
            f"- Verified success: **{_fmt(r.get('verified_success'))}** "
            f"(failed steps: {', '.join(r['verification'].get('failed_steps') or []) or 'none'})",
            f"- Repeated known failed approach: **{_fmt(r['repeated_known_failure'].get('matched'))}**",
            f"- Agent exit: {r['agent_exit']['status']} · reported completion: {_fmt(r['agent_exit'].get('agent_reported_completion'))}"
            f" · turns: {_fmt(r['agent_exit'].get('num_turns'))}",
            f"- Wall clock: {_fmt(r['timing']['wall_clock_seconds'])}s",
            f"- OpenShard history present: {_fmt(r['openshard'].get('history_present'))} · MCP server: "
            f"{_fmt(r['openshard'].get('mcp_server_kind'))} ({_fmt(r['openshard'].get('mcp_server_status'))})"
            f" · retrieval observed: {r['openshard'].get('retrieval_observed')}",
            f"- OpenShard tools called: {', '.join(r['openshard'].get('tools_called') or []) or 'none'}",
            f"- Files changed: {', '.join(r['activity']['files_changed'].get('modified', []) + r['activity']['files_changed'].get('added', [])) or 'none'}",
            f"- Cost (Claude-reported): {_fmt(r['usage'].get('total_cost_usd'))} · model(s): "
            f"{', '.join(r['model']['observed_in_assistant_messages']) or 'unknown'}",
            f"- Errors: {'; '.join(r.get('errors') or []) or 'none'}",
            f"- Run dir: `{r['run_dir']}`",
            "",
        ]
    lines += ["## Caveats", ""] + [f"- {cv}" for cv in comparison["caveats"]] + [""]
    return "\n".join(lines)
