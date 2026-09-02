from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from openshard.history.shard_schema import coerce_shard_entry

_LOG_PATH = Path(".openshard") / "runs.jsonl"

ALL_PROFILES = ("native_light", "native_deep", "native_swarm")


def load_runs(repo_path: Path | None = None) -> list[dict]:
    log_path = (repo_path or Path.cwd()) / _LOG_PATH
    if not log_path.exists():
        return []
    runs: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(coerce_shard_entry(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return runs


def compute_model_stats(runs: list[dict]) -> dict[str, dict]:
    """Return per-model performance stats, sorted by runs_count descending."""
    accum: dict[str, dict] = defaultdict(lambda: {
        "runs_count": 0,
        "total_cost": 0.0,
        "cost_count": 0,
        "total_duration": 0.0,
        "duration_count": 0,
        "total_tokens": 0,
        "token_count": 0,
        "verif_passed": 0,
        "verif_failed": 0,
        "retry_count": 0,
        "last_used": None,
    })

    for run in runs:
        model = run.get("execution_model")
        if not model:
            continue
        s = accum[model]
        s["runs_count"] += 1

        cost = run.get("estimated_cost")
        if cost is not None:
            s["total_cost"] += cost
            s["cost_count"] += 1

        duration = run.get("duration_seconds")
        if duration is not None:
            s["total_duration"] += duration
            s["duration_count"] += 1

        tokens = run.get("total_tokens")
        if tokens is not None:
            s["total_tokens"] += tokens
            s["token_count"] += 1

        vp = run.get("verification_passed")
        if vp is True:
            s["verif_passed"] += 1
        elif vp is False:
            s["verif_failed"] += 1

        if run.get("retry_triggered"):
            s["retry_count"] += 1

        ts = run.get("timestamp")
        if ts and (s["last_used"] is None or ts > s["last_used"]):
            s["last_used"] = ts

    result: dict[str, dict] = {}
    for model, s in sorted(accum.items(), key=lambda x: x[1]["runs_count"], reverse=True):
        n = s["runs_count"]
        verif_total = s["verif_passed"] + s["verif_failed"]
        result[model] = {
            "runs_count": n,
            "avg_cost": s["total_cost"] / s["cost_count"] if s["cost_count"] else None,
            "avg_duration": s["total_duration"] / s["duration_count"] if s["duration_count"] else None,
            "avg_tokens": round(s["total_tokens"] / s["token_count"]) if s["token_count"] else None,
            "verification_pass_rate": s["verif_passed"] / verif_total if verif_total else None,
            "retry_rate": s["retry_count"] / n,
            "last_used_timestamp": s["last_used"],
        }

    return result


def compute_skill_stats(runs: list[dict]) -> dict[str, dict]:
    """Return per-skill performance stats from run history.

    Runs without matched_skills are ignored. A run contributes to every skill
    slug it was matched against.  Results are sorted by runs_count descending.
    """
    accum: dict[str, dict] = defaultdict(lambda: {
        "runs_count": 0,
        "total_cost": 0.0, "cost_count": 0,
        "total_duration": 0.0, "duration_count": 0,
        "verif_passed": 0, "verif_failed": 0,
        "retry_count": 0,
    })

    for run in runs:
        slugs = run.get("matched_skills")
        if not slugs:
            continue
        for slug in slugs:
            s = accum[slug]
            s["runs_count"] += 1

            cost = run.get("estimated_cost")
            if cost is not None:
                s["total_cost"] += cost
                s["cost_count"] += 1

            duration = run.get("duration_seconds")
            if duration is not None:
                s["total_duration"] += duration
                s["duration_count"] += 1

            vp = run.get("verification_passed")
            if vp is True:
                s["verif_passed"] += 1
            elif vp is False:
                s["verif_failed"] += 1

            if run.get("retry_triggered"):
                s["retry_count"] += 1

    result: dict[str, dict] = {}
    for slug, s in sorted(accum.items(), key=lambda x: x[1]["runs_count"], reverse=True):
        n = s["runs_count"]
        verif_total = s["verif_passed"] + s["verif_failed"]
        result[slug] = {
            "runs_count": n,
            "avg_cost": s["total_cost"] / s["cost_count"] if s["cost_count"] else None,
            "avg_duration": s["total_duration"] / s["duration_count"] if s["duration_count"] else None,
            "verification_pass_rate": s["verif_passed"] / verif_total if verif_total else None,
            "retry_rate": s["retry_count"] / n if n else None,
        }
    return result


def compute_profile_stats(runs: list[dict]) -> dict[str, dict]:
    """Return per-profile performance stats. All three profiles are always present."""
    accum = {p: {
        "runs_count": 0,
        "total_cost": 0.0, "cost_count": 0,
        "total_duration": 0.0, "duration_count": 0,
        "verif_passed": 0, "verif_failed": 0,
        "retry_count": 0,
    } for p in ALL_PROFILES}

    for run in runs:
        profile = run.get("execution_profile")
        if not profile or profile not in accum:
            continue
        s = accum[profile]
        s["runs_count"] += 1

        cost = run.get("estimated_cost")
        if cost is not None:
            s["total_cost"] += cost
            s["cost_count"] += 1

        duration = run.get("duration_seconds")
        if duration is not None:
            s["total_duration"] += duration
            s["duration_count"] += 1

        vp = run.get("verification_passed")
        if vp is True:
            s["verif_passed"] += 1
        elif vp is False:
            s["verif_failed"] += 1

        if run.get("retry_triggered"):
            s["retry_count"] += 1

    result: dict[str, dict] = {}
    for profile in ALL_PROFILES:
        s = accum[profile]
        n = s["runs_count"]
        verif_total = s["verif_passed"] + s["verif_failed"]
        result[profile] = {
            "runs_count": n,
            "avg_cost": s["total_cost"] / s["cost_count"] if s["cost_count"] else None,
            "avg_duration": s["total_duration"] / s["duration_count"] if s["duration_count"] else None,
            "verification_pass_rate": s["verif_passed"] / verif_total if verif_total else None,
            "retry_rate": s["retry_count"] / n if n else None,
        }
    return result
