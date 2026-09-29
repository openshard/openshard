from __future__ import annotations

from openshard.providers.openrouter import compute_cost

BASELINE_MODELS: list[tuple[str, str]] = [
    ("GPT-5.6 Sol", "openai/gpt-5.6-sol"),
    ("Sonnet 4.6", "anthropic/claude-sonnet-4.6"),
]


def _multiplier_suffix(actual_cost: float, baseline_cost: float) -> str:
    ratio = baseline_cost / actual_cost
    if ratio < 1.05:
        return ""
    if ratio >= 10:
        return f" (~{round(ratio)}x higher)"
    return f" (~{ratio:.1f}x higher)"


def format_baseline_line(
    prompt_tokens: int,
    completion_tokens: int,
    actual_cost: float | None = None,
    models: list[tuple[str, str]] | None = None,
) -> str | None:
    """Return a formatted baseline estimate line, or None if it should be omitted.

    Omitted when tokens are both zero/falsy, or no baseline model can be priced.
    Adds a (~Nx higher) suffix per entry when actual_cost > 0.
    """
    if not prompt_tokens and not completion_tokens:
        return None
    if models is None:
        models = BASELINE_MODELS
    parts: list[str] = []
    for label, model_id in models:
        cost = compute_cost(model_id, prompt_tokens, completion_tokens)
        if cost is None:
            continue
        entry = f"{label} ${cost:.3f}"
        if actual_cost and actual_cost > 0:
            entry += _multiplier_suffix(actual_cost, cost)
        parts.append(entry)
    if not parts:
        return None
    return "Baseline estimate: " + ", ".join(parts)


FRONTIER_BASELINE_MODEL = "anthropic/claude-sonnet-4.6"

FULL_COMPARISON_MODELS: list[tuple[str, str]] = [
    ("Sonnet 4.6", "anthropic/claude-sonnet-4.6"),
    ("GPT-5.5",    "openai/gpt-5.5"),
    ("Opus 4.8",   "anthropic/claude-opus-4.8"),
]


def format_cost_difference(actual: float, baseline: float) -> str:
    """Full directional phrase for a cost comparison.

    Returns one of:
      '$0.0071 cheaper (68%, 3.2x lower cost)'
      '$0.0006 more expensive (7%, 1.1x higher cost)'
      '$X.XXXX cheaper (100%)'   when actual==0 and baseline>0, no x-multiple
      'equal'
    """
    if baseline <= 0 or actual == baseline:
        return "equal"
    if actual <= 0:
        return f"${baseline:.4f} cheaper (100%)"
    saving = baseline - actual
    if saving > 0:
        pct = round(saving / baseline * 100)
        x = baseline / actual
        x_str = f"{round(x)}x" if x >= 10 else f"{x:.1f}x"
        return f"${saving:.4f} cheaper ({pct}%, {x_str} lower cost)"
    else:
        excess = -saving
        pct = round(excess / baseline * 100)
        x = actual / baseline
        x_str = f"{round(x)}x" if x >= 10 else f"{x:.1f}x"
        return f"${excess:.4f} more expensive ({pct}%, {x_str} higher cost)"


def format_full_comparison_lines(
    prompt_tokens: int,
    completion_tokens: int,
    actual_cost: float,
) -> list[str]:
    """Return one formatted line per priceable model in FULL_COMPARISON_MODELS.

    Each line: '    {label}-only:  ${bc:.4f}  {format_cost_difference(actual_cost, bc)}'
    Models where compute_cost returns None are silently skipped.
    """
    lines: list[str] = []
    for label, model_id in FULL_COMPARISON_MODELS:
        bc = compute_cost(model_id, prompt_tokens, completion_tokens)
        if bc is None:
            continue
        diff = format_cost_difference(actual_cost, bc)
        lines.append(f"    {label}-only:  ${bc:.4f}  {diff}")
    return lines


def _ratio_str(actual: float, baseline: float) -> str:
    if actual <= 0 or baseline <= actual:
        return ""
    ratio = baseline / actual
    if ratio >= 10:
        return f"{round(ratio)}x higher"
    return f"{ratio:.1f}x higher"


def format_concise_comparison_lines(
    prompt_tokens: int,
    completion_tokens: int,
    actual_cost: float,
) -> list[str]:
    """Return one concise line per priceable baseline model.

    Each line: '    {label}-only   ${bc:.4f}   {ratio}x higher'
    """
    lines: list[str] = []
    for label, model_id in FULL_COMPARISON_MODELS:
        bc = compute_cost(model_id, prompt_tokens, completion_tokens)
        if bc is None:
            continue
        ratio = _ratio_str(actual_cost, bc)
        suffix = f"   {ratio}" if ratio else ""
        lines.append(f"    {label}-only   ${bc:.4f}{suffix}")
    return lines


def compute_baseline_comparison(
    prompt_tokens: int,
    completion_tokens: int,
    actual_cost: float | None,
) -> dict | None:
    """Return a cost comparison dict, or None if it cannot be calculated.

    Returns None when tokens are both zero, actual_cost is None, or the
    frontier baseline model cannot be priced. estimated_saving_percent is
    None when actual_cost is zero (avoids division by zero).
    """
    if not prompt_tokens and not completion_tokens:
        return None
    if actual_cost is None:
        return None
    baseline_cost = compute_cost(FRONTIER_BASELINE_MODEL, prompt_tokens, completion_tokens)
    if baseline_cost is None:
        return None
    saving = baseline_cost - actual_cost
    percent: int | None = round(saving / baseline_cost * 100) if actual_cost > 0 else None
    return {
        "actual_cost_usd": actual_cost,
        "frontier_baseline_cost_usd": baseline_cost,
        "estimated_saving_usd": saving,
        "estimated_saving_percent": percent,
    }
