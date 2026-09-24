from __future__ import annotations

from openshard.models.registry import (
    all_models,
    display_name_for,
    models_by_capability,
    models_by_role,
)

_ASK_FALLBACK = (
    "Ask Mode v1 can answer OpenShard/product questions only.\n"
    "For repo analysis, use a normal task or /pack.\n"
    "For planning, use /plan <task>."
)

_COMMANDS_TEXT = (
    "TUI commands:\n"
    "  /help                    Show help\n"
    "  /last                    Show the most recent run\n"
    "  /last more               Show more detail for the last run\n"
    "  /last full               Show full debug/audit detail\n"
    "  /feedback <outcome>      Record outcome for the last run\n"
    "  /clear                   Clear the output panel\n"
    "  /quit                    Exit the TUI\n"
    "  /packs                   List available workflow packs\n"
    "  /pack <id>               Load a workflow pack\n"
    "  /ask <question>          Ask OpenShard a product question\n"
    "  /plan <task>             Generate a local execution plan\n"
    "\n"
    "Plain text is sent to the execution engine as a task.\n"
    "\n"
    "CLI commands (run in terminal):\n"
    "  openshard reflect last\n"
    "  openshard pr comment\n"
    "  openshard pr comment --output pr-comment.md"
)

_OPENSHARD_TEXT = (
    "OpenShard is a local CLI and TUI — the control layer for AI coding agents.\n"
    "It routes tasks to AI models, tracks structured run receipts (shards),\n"
    "and provides model routing, evidence provenance, and session history.\n"
    "\n"
    "Run `openshard --help` for full CLI options, or /help inside the TUI."
)

_RECEIPT_TEXT = (
    "A shard receipt is the structured record of a completed OpenShard run.\n"
    "It includes:\n"
    "  RESULT          — outcome and verification status\n"
    "  ACTIONS         — file and code operations performed\n"
    "  CHECK ACTIONS   — IaC, lint, or format verification steps\n"
    "  EVIDENCE        — files inspected, changed, or sourced\n"
    "\n"
    "Receipts are stored in .openshard/runs.jsonl.\n"
    "Use /last, /last more, or /last full to view them in the TUI."
)

_LAST_TEXT = (
    "  /last           - show the most recent run summary\n"
    "  /last more      - include check actions and evidence\n"
    "  /last full      - show the complete raw run output\n"
    "  /feedback       - record your outcome for the last run\n"
    "                    (accepted / rejected / partial / abandoned / retried)\n"
    "\n"
    "CLI commands (run in terminal):\n"
    "  openshard reflect last          - structured run reflection\n"
    "  openshard pr comment            - generate a GitHub PR comment\n"
    "  openshard pr comment --output pr-comment.md"
)

_PLAN_TEXT = (
    "/plan generates a structured execution plan before you commit to a run.\n"
    "Use it to review approach, scope, and risk notes.\n"
    "Example: /plan refactor the auth module\n"
    "No provider calls are made — plan output is local and instant."
)

_PACK_TEXT = (
    "Context packs attach a structured suffix and workflow to your next task.\n"
    "  /packs          — list available packs\n"
    "  /pack <id>      — select a pack and activate it for the next run\n"
    "Once activated, the pack context is appended to your task automatically."
)


def answer_ask_mode(question: str) -> str:
    """Return a local, deterministic answer for an Ask Mode question. No provider calls."""
    q = question.strip().lower()

    if "what models" in q or "which models" in q or "model roster" in q:
        return _answer_model_roster()
    if (
        "cheap control" in q
        or "cheap model" in q
        or "low cost" in q
        or "low-cost" in q
        or "lightweight" in q
        or "control model" in q
    ):
        return _answer_cheap_control()
    if "reasoning model" in q or "reasoning capable" in q:
        return _answer_reasoning_models()
    if "experimental model" in q or "experimental" in q:
        return _answer_experimental_models()
    if "what commands" in q or "available commands" in q:
        return _COMMANDS_TEXT
    if "what is openshard" in q or "what does openshard" in q or "about openshard" in q:
        return _OPENSHARD_TEXT
    if "receipt" in q or "shard receipt" in q or "what is a shard" in q:
        return _RECEIPT_TEXT
    if "/last" in q or "what is last" in q or "what does last" in q:
        return _LAST_TEXT
    if "/plan" in q or "what is plan" in q:
        return _PLAN_TEXT
    if (
        "model policy" in q
        or ("ask mode" in q and "model" in q)
        or ("plan mode" in q and "model" in q)
    ):
        return _answer_mode_policy()
    if "/pack" in q or "context pack" in q:
        return _PACK_TEXT
    return _ASK_FALLBACK


_ROSTER_FRONTIER = (
    "openai/gpt-5.5",
    "openai/gpt-5.5-pro",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
)
_ROSTER_VALUE = (
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.1",
    "qwen/qwen3.7-max",
)
_ROSTER_CHEAP = (
    "deepseek/deepseek-v4-flash",
    "openai/gpt-5-nano",
    "google/gemini-3.1-flash-lite",
    "ibm-granite/granite-4.1-8b",
)


def _answer_model_roster() -> str:
    count = len(all_models())
    lines = [
        f"OpenShard currently knows {count} registered models.",
        "",
        "OpenShard groups models by what they are useful for:",
        "  - Frontier / escalation",
        "  - Long-horizon / value work",
        "  - Planning and review",
        "  - Reasoning-heavy tasks",
        "  - Low-cost / control",
        "  - Experimental coding agents",
        "",
        "Frontier / escalation examples:",
    ]
    for mid in _ROSTER_FRONTIER:
        lines.append(f"  {display_name_for(mid)}")
    lines += ["", "Long-horizon / value examples:"]
    for mid in _ROSTER_VALUE:
        lines.append(f"  {display_name_for(mid)}")
    lines += ["", "Low-cost / control examples:"]
    for mid in _ROSTER_CHEAP:
        lines.append(f"  {display_name_for(mid)}")
    lines += ["", "Current routing-class defaults:"]
    lines += _routing_class_default_lines()
    lines += [
        "",
        "Use:",
        "  /ask low-cost models",
        "  /ask reasoning models",
        "  /ask experimental models",
        "  openshard models list",
        "  openshard models classes",
    ]
    return "\n".join(lines)


def _routing_class_default_lines() -> list[str]:
    try:
        from openshard.models.catalog import curated_catalog
        from openshard.routing.routing_classes import select_all_classes

        selections = select_all_classes(curated_catalog())
    except Exception:
        return ["  (unavailable)"]
    return [
        f"  {name:<20}{display_name_for(sel.model) if sel.model else '-'}"
        for name, sel in selections.items()
    ]


def _answer_cheap_control() -> str:
    models = models_by_role("cheap_control")
    if not models:
        return "No low-cost / control models found in the current registry."
    col = max(len(m.display_name) for m in models) + 4
    lines = ["Low-cost / control models:", ""]
    for m in models:
        lines.append(f"  {m.display_name:<{col}}{m.cost_class} / {m.latency_class}")
    lines += ["", "Use `openshard models role cheap_control` for the raw registry role."]
    return "\n".join(lines)


def _answer_reasoning_models() -> str:
    models = models_by_capability("reasoning")
    if not models:
        return "No reasoning-capable models found in the current registry."
    lines = ["Reasoning-capable models:", ""]
    for m in models:
        lines.append(f"  {m.display_name}")
    lines += ["", "Use `openshard models capabilities reasoning` for full details."]
    return "\n".join(lines)


def _answer_experimental_models() -> str:
    models = [m for m in all_models() if m.experimental]
    if not models:
        return "No experimental models in the current registry."
    lines = ["Experimental models:", ""]
    for m in models:
        lines.append(f"  {m.display_name}")
    lines += ["", "Use `openshard models experimental` for full details."]
    return "\n".join(lines)


def _answer_mode_policy() -> str:
    from openshard.models.mode_policy import model_policy_for_mode

    ask = model_policy_for_mode("ask")
    plan = model_policy_for_mode("plan")
    if ask is None or plan is None:
        return "Model policy is unavailable."
    lines = [
        "Ask Mode and Plan Mode are currently local deterministic.",
        "No provider calls are made. Model policy is advisory only.",
        "",
        "Ask Mode model policy (advisory only):",
        f"  Default   {display_name_for(ask.default_model_id)}",
        "  Fallbacks " + ", ".join(display_name_for(fid) for fid in ask.fallback_model_ids),
        "",
        "Plan Mode model policy (advisory only):",
        f"  Default   {display_name_for(plan.default_model_id)}",
        "  Fallbacks " + ", ".join(display_name_for(fid) for fid in plan.fallback_model_ids),
        "",
        "Run routing remains controlled by the existing routing policy.",
        "",
        "Use `openshard models mode ask` or `openshard models mode plan` for CLI details.",
    ]
    return "\n".join(lines)
