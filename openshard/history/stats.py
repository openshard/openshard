"""Lightweight, honest local stats over recorded Shards (``openshard stats``).

Everything here is a *count of what was recorded* -- derived only from the
canonical receipts ``history.query.recent_shards`` already builds. There is
deliberately no productivity score, efficiency percentage, or any formula
that would turn recorded facts into a judgement about how well the work
went. Missing data is reported as missing (``cost_missing_shards``,
``models["unknown"]``) rather than filled in.

Cost is always labelled as an estimate: OpenShard-calculated figures come
from token counts times list prices and agent-reported figures (Claude
Code's status line) are documented as approximate, so neither is billing
truth. Token totals include only provider-reported counts
(``tokens_provenance`` set on the receipt), never inferred ones.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from openshard.history.proof_signals import verification_status_from_receipt
from openshard.history.query import RecentShard
from openshard.history.shard_contract import ShardReceipt

MODEL_UNKNOWN = "unknown"
_TOP_FILES = 5

_TASK_COMPLETION_KEYS: dict[str, str] = {
    "Completed": "completed",
    "In progress": "in_progress",
    "Ended (no turn observed)": "ended_no_turn",
}


@dataclass
class HistoryStats:
    shards: int = 0
    attempts: int = 0
    retried_shards: int = 0
    first_at: str | None = None
    last_at: str | None = None
    agents: dict[str, int] = field(default_factory=dict)
    origins: dict[str, int] = field(default_factory=dict)
    capture_depths: dict[str, int] = field(default_factory=dict)
    # Shards per model display name; a Shard observed across several models
    # counts once under each of them. "unknown" is an explicit bucket.
    models: dict[str, int] = field(default_factory=dict)
    verification: dict[str, int] = field(default_factory=dict)
    # Only Shards that carry a turn-completion signal (Claude Code capture).
    task_completion: dict[str, int] = field(default_factory=dict)
    cost_total_usd: float | None = None
    cost_shards: int = 0
    cost_provider_reported_shards: int = 0
    cost_missing_shards: int = 0
    tokens_input: int | None = None
    tokens_output: int | None = None
    tokens_cache_read: int | None = None
    tokens_shards: int = 0
    duration_total_seconds: float | None = None
    duration_shards: int = 0
    files_changed_total: int = 0
    files_changed_shards: int = 0
    top_files: list[tuple[str, int]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "shards": self.shards,
            "attempts": self.attempts,
            "retried_shards": self.retried_shards,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "agents": dict(self.agents),
            "origins": dict(self.origins),
            "capture_depths": dict(self.capture_depths),
            "models": dict(self.models),
            "verification": dict(self.verification),
            "task_completion": dict(self.task_completion),
            "cost": {
                "total_usd": self.cost_total_usd,
                "is_estimate": True,
                "shards_with_cost": self.cost_shards,
                "shards_provider_reported": self.cost_provider_reported_shards,
                "shards_missing": self.cost_missing_shards,
            },
            "tokens": {
                "input": self.tokens_input,
                "output": self.tokens_output,
                "cache_read": self.tokens_cache_read,
                "shards_with_tokens": self.tokens_shards,
                "provenance": "provider_reported",
            },
            "duration": {
                "total_seconds": self.duration_total_seconds,
                "shards_with_duration": self.duration_shards,
            },
            "files": {
                "changed_total": self.files_changed_total,
                "shards_with_changes": self.files_changed_shards,
                "top": [{"path": p, "shards": n} for p, n in self.top_files],
            },
        }


def _model_names(receipt: ShardReceipt) -> list[str]:
    """Distinct model display names behind a receipt, normalised for counting."""
    raw: list[str] = []
    if receipt.model_stages:
        raw = list(dict.fromkeys(m for _, m in receipt.model_stages))
    elif receipt.model_display:
        raw = [receipt.model_display]
    out: list[str] = []
    for name in raw:
        cleaned = name.removeprefix("Auto → ").strip()
        if not cleaned or cleaned.lower() in ("unknown", "not recorded"):
            cleaned = MODEL_UNKNOWN
        if cleaned not in out:
            out.append(cleaned)
    return out or [MODEL_UNKNOWN]


def _sorted_counts(counter: Counter[str]) -> dict[str, int]:
    """Descending by count, then name; ``unknown`` always last."""
    return dict(sorted(
        counter.items(),
        key=lambda kv: (kv[0] == MODEL_UNKNOWN, -kv[1], kv[0]),
    ))


def compute_history_stats(items: list[RecentShard], *, total_attempts: int | None = None) -> HistoryStats:
    """Aggregate *items* (as returned by ``recent_shards``) into ``HistoryStats``.

    Pure and deterministic. ``total_attempts`` overrides the attempt total
    when the caller knows it from the full history (``HistoryPage``).
    """
    stats = HistoryStats()
    stats.shards = len(items)
    stats.attempts = total_attempts if total_attempts is not None else sum(i.attempt_count for i in items)
    if not items:
        return stats

    agents: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    depths: Counter[str] = Counter()
    models: Counter[str] = Counter()
    verification: Counter[str] = Counter()
    completion: Counter[str] = Counter()
    files: Counter[str] = Counter()

    cost_total = 0.0
    tokens_in = tokens_out = tokens_cache = 0
    duration_total = 0.0
    timestamps: list[str] = []

    for item in items:
        r = item.receipt
        shard = item.shard
        if item.attempt_count > 1:
            stats.retried_shards += 1
        if shard.created_at:
            timestamps.append(shard.created_at)

        agents[shard.agent or "unknown"] += 1
        origins[shard.origin or "unknown"] += 1
        depths[shard.capture_depth or "unknown"] += 1
        for name in _model_names(r):
            models[name] += 1
        verification[verification_status_from_receipt(r)] += 1
        if r.task_completion:
            completion[_TASK_COMPLETION_KEYS.get(r.task_completion, r.task_completion)] += 1

        if r.cost_raw is not None:
            cost_total += float(r.cost_raw)
            stats.cost_shards += 1
            if r.cost_provenance:
                stats.cost_provider_reported_shards += 1
        else:
            stats.cost_missing_shards += 1

        if r.tokens_provenance and (r.tokens_input is not None or r.tokens_output is not None):
            stats.tokens_shards += 1
            tokens_in += r.tokens_input or 0
            tokens_out += r.tokens_output or 0
            tokens_cache += r.tokens_cache_read or 0

        if isinstance(r.duration_seconds, (int, float)) and not isinstance(r.duration_seconds, bool):
            duration_total += float(r.duration_seconds)
            stats.duration_shards += 1

        if r.files_changed:
            stats.files_changed_total += int(r.files_changed)
            stats.files_changed_shards += 1
        for path in dict.fromkeys(r.files_touched):
            if isinstance(path, str) and path:
                files[path] += 1

    stats.first_at = min(timestamps) if timestamps else None
    stats.last_at = max(timestamps) if timestamps else None
    stats.agents = _sorted_counts(agents)
    stats.origins = _sorted_counts(origins)
    stats.capture_depths = _sorted_counts(depths)
    stats.models = _sorted_counts(models)
    stats.verification = _sorted_counts(verification)
    stats.task_completion = _sorted_counts(completion)
    stats.cost_total_usd = round(cost_total, 6) if stats.cost_shards else None
    if stats.tokens_shards:
        stats.tokens_input = tokens_in
        stats.tokens_output = tokens_out
        stats.tokens_cache_read = tokens_cache
    stats.duration_total_seconds = round(duration_total, 2) if stats.duration_shards else None
    stats.top_files = sorted(files.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_FILES]
    return stats
