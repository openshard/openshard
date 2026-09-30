"""Learning signals: structured, evidence-backed observations over prior Receipts.

A signal is a deterministic aggregate over *observations* (one per Receipt).
It carries its scope (repository, task category), its subject (a model, a
recovery path, a check, a failure category, a policy boundary), counts, the
Receipt/Shard ids that support it, how fresh it is and how strong its sample
is. The one-line ``summary`` is rendered from those fields; the structured
fields are the source of truth.

Evidence rules
--------------
* Only outcomes OpenShard (or an independent system) observed count as a pass
  or a failure: ``routing.adaptive.outcome.verified_success_for``. An agent's
  own claim, an imported transcript, or an unknown status is never evidence
  either way; such Receipts are counted as ``excluded`` and nothing more.
* Samples are distinct Receipts (deduplicated by ``receipt_id``), never
  ``shard_id`` groups: hook-captured shard ids are per-machine sequences that
  collide across agents and repositories, and grouping by them would invent
  retries.
* Missing cost stays missing. A cost per verified success is reported only
  when every Receipt in the sample has a provider-reported cost and the sample
  meets ``MIN_COST_SAMPLES``.
* A failed check proves the check failed, not that a model caused it. Model
  signals say what was observed ("passed verification on the first attempt in
  2 of 3 runs"), never that a model is better or worse.
* A single Receipt is an anecdote: ``strength`` is ``anecdotal`` and retrieval
  never surfaces it.

Privacy
-------
Signals hold counts, enum tokens, model ids, repo-relative directory areas,
content terms from the already-sanitised task text, and ids. Never prompts,
model replies, command output, error messages, absolute paths or secrets. A
verify command appears only as the label ``record.check_identity`` judged
safe, plus a fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openshard.safety.sanitize import is_absolute_path, looks_like_secret, sanitize_text

LEARNING_SIGNAL_VERSION = 1

KIND_MODEL_OUTCOMES = "model_task_outcomes"
KIND_RECOVERY = "recovery_path"
KIND_CHECK = "recurring_check_failure"
KIND_TEST = "recurring_test_failure"
KIND_FAILURE = "recurring_failure"
KIND_POLICY = "policy_boundary"
KINDS = (KIND_MODEL_OUTCOMES, KIND_RECOVERY, KIND_CHECK, KIND_TEST, KIND_FAILURE, KIND_POLICY)

STRENGTH_ANECDOTAL = "anecdotal"  # one Receipt: never surfaced
STRENGTH_WEAK = "weak"
STRENGTH_MODERATE = "moderate"
STRENGTH_STRONG = "strong"

FRESH = "fresh"
AGING = "aging"
STALE = "stale"
FRESH_DAYS = 30
STALE_AFTER_DAYS = 90

MIN_COST_SAMPLES = 3
MAX_ENTRIES_SCANNED = 5000
MAX_REFS = 10
MAX_TERMS = 16
MAX_AREAS = 6

# Verification sources that count as an observed outcome.
_OBSERVED_SOURCES = frozenset({"directly_observed", "git_verified", "independently_verified"})

# OSN stop reasons -> failure category. Only reasons OpenShard itself observed.
_OSN_FAILURES = {
    "max_attempts_exhausted": "verification_failed",
    "no_progress_identical_failure": "verification_failed",
    "no_progress_identical_actions": "no_progress",
    "verifier_setup_failed": "verification_infra_error",
    "verifier_timeout": "timed_out",
    "verifier_modified_files": "verifier_modified_files",
    "provider_error": "provider_error",
    "policy_or_path_block": "policy_blocked",
    "verification_command_policy_block": "policy_blocked",
    "provider proposed no actions": "model_no_changes",
}
# Categories that describe the environment or the provider, not the change.
OPERATIONAL_FAILURES = frozenset({
    "verification_infra_error", "timed_out", "provider_error", "verifier_modified_files",
})

FAILURE_LABELS = {
    "verification_failed": "failed OpenShard-run verification",
    "no_progress": "stopped because a retry proposed identical changes",
    "verification_infra_error": "could not run the verifier (environment/setup)",
    "timed_out": "timed out before verification produced a verdict",
    "verifier_modified_files": "had a verifier that modified the files it checked",
    "provider_error": "hit a provider error",
    "policy_blocked": "were stopped by a policy block",
    "model_no_changes": "ended with no usable model changes",
}


# ---------------------------------------------------------------------------
# Observations: one per Receipt
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptObservation:
    n: int
    model: str | None
    state: str | None  # passed | failed | unknown | not_run | None (nothing recorded)
    failed_tests: tuple[str, ...] = ()  # test ids the verifier named, failed attempts only


@dataclass(frozen=True)
class CheckObservation:
    fingerprint: str
    label: str
    kind: str
    failed_attempts: int
    passed: bool | None  # the check's last observed state in the run


@dataclass(frozen=True)
class Observation:
    """What one Receipt contributes to learning. Every field is observed or None."""

    receipt_id: str
    shard_id: str | None
    timestamp: datetime | None
    repo: str | None
    task_category: str | None
    category_source: str | None
    harness: str | None
    terms: tuple[str, ...]
    areas: tuple[str, ...]
    final_model: str | None
    verified_success: bool | None
    verification_source: str | None
    excluded_reason: str | None  # why the outcome is not evidence, when it is not
    attempts: tuple[AttemptObservation, ...]
    cost_usd: float | None
    duration_seconds: float | None
    retried: bool | None
    failure_category: str | None
    checks: tuple[CheckObservation, ...]
    policy_decisions: tuple[tuple[str, str], ...]  # (decision, area)
    approval_outcome: str | None
    learning_used: bool | None  # this run itself consulted learning (None: not recorded)
    learning_signal_ids: tuple[str, ...] = ()

    @property
    def first_model(self) -> str | None:
        return self.attempts[0].model if self.attempts else None

    @property
    def observed(self) -> bool:
        return self.verified_success is not None


def _dict(v: object) -> dict:
    return v if isinstance(v, dict) else {}


def _str(v: object) -> str | None:
    return v if isinstance(v, str) and v else None


def _float(v: object) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if f == f and f not in (float("inf"), float("-inf")) else None


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=UTC)


_TERM_OK = re.compile(r"^[a-z][a-z0-9]{2,23}$")


def task_terms(task: object) -> tuple[str, ...]:
    """Content terms of a task for relevance, privacy-filtered and bounded.

    Reuses ``relevant_context``'s tokenizer and generic-term filter so both
    retrieval surfaces agree on what a "content word" is. Terms that could be
    an identifier or key (long letter/digit runs) are dropped, not stored.
    """
    from openshard.history.query import _query_terms

    if not isinstance(task, str):
        return ()
    # Whole tokens that look like a key or a local path go before tokenizing,
    # or "sk-<key>" would split into an innocent-looking word.
    kept = " ".join(
        tok for tok in task.split()
        if not looks_like_secret(tok) and not is_absolute_path(tok.strip("\"'()[]<>,;"))
    )
    out: list[str] = []
    for t in _query_terms(kept):
        if not _TERM_OK.match(t) or looks_like_secret(t):
            continue
        if len(t) >= 12 and any(ch.isdigit() for ch in t):
            continue
        out.append(t)
        if len(out) >= MAX_TERMS:
            break
    return tuple(out)


def path_area(path: object) -> str | None:
    """The repo-relative directory area of *path* (at most two segments), or None."""
    if not isinstance(path, str) or not path or path == "<unsafe-path>":
        return None
    norm = path.replace("\\", "/").strip()
    if is_absolute_path(norm) or norm.startswith("~"):
        return None
    parts = [p for p in norm.split("/") if p and p != "."]
    if not parts or ".." in parts or any(looks_like_secret(p) for p in parts):
        return None
    dirs = parts[:-1]
    area = "/".join(dirs[:2]) if dirs else "."
    return sanitize_text(area, 80)


def _areas(paths: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for p in paths:
        a = path_area(p)
        if a and a not in out:
            out.append(a)
        if len(out) >= MAX_AREAS:
            break
    return tuple(out)


def _entry_paths(entry: dict) -> list[str]:
    paths = [f.get("path") for f in entry.get("files_detail") or [] if isinstance(f, dict)]
    if not paths:
        paths = list(_dict(entry.get("diff_review")).get("changed_files") or [])
    return [p for p in paths if isinstance(p, str)]


def task_category_for(entry: dict) -> tuple[str | None, str | None]:
    """The task category the routing layer recorded, else the keyword classifier over
    the stored task text (the same classifier, so the label means the same thing)."""
    ctx = _dict(_dict(entry.get("routing_provenance")).get("context"))
    recorded = _str(ctx.get("task_category"))
    if recorded:
        return recorded, _str(ctx.get("category_source")) or "recorded"
    task = _str(entry.get("task"))
    if not task:
        return None, None
    try:
        from openshard.routing.engine import route

        return route(task).category, "keyword_classifier"
    except Exception:
        return None, None


def _attempt_state(v: object) -> str | None:
    if not isinstance(v, dict):
        return None
    if v.get("setup_failure") or v.get("ran") is False:
        return "not_run"
    if v.get("timed_out") or v.get("tainted"):
        # No verdict on the change: a timeout, or a verifier that rewrote what it checked.
        return "unknown"
    passed = v.get("passed")
    return "passed" if passed is True else "failed" if passed is False else None


def _failed_tests(v: object) -> tuple[str, ...]:
    from openshard.verification.failed_tests import MAX_FAILED_TESTS, safe_test_id

    raw = v.get("failed_tests") if isinstance(v, dict) else None
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    for t in raw:
        # Re-checked on read: a stored id is only trusted if it is still a safe, relative id.
        clean = safe_test_id(t)
        if clean and clean not in out:
            out.append(clean)
    return tuple(out[:MAX_FAILED_TESTS])


def _osn_attempts(entry: dict) -> tuple[AttemptObservation, ...]:
    """Per-attempt models and verification states of an OSN run.

    The model of each attempt is taken from what the run recorded: the
    ``learning.attempt_models`` list (new Receipts), else attempt 1 from the
    final model when no retry happened or from the applied routing decision's
    executed model, and later attempts from ``retry_attempts``. Anything else
    is unknown, never guessed.
    """
    loop = _dict(entry.get("osn_loop"))
    raw = [a for a in loop.get("attempts") or [] if isinstance(a, dict)]
    if not raw:
        return ()
    recorded = {
        int(m["attempt"]): _str(m.get("model"))
        for m in _dict(entry.get("learning")).get("attempt_models") or []
        if isinstance(m, dict) and isinstance(m.get("attempt"), int) and not isinstance(m.get("attempt"), bool)
    }
    retried = entry.get("retry_triggered") is True
    prov = _dict(entry.get("routing_provenance"))
    first = recorded.get(1)
    if first is None:
        if not retried:
            first = _str(entry.get("execution_model"))
        elif prov.get("record_mode") == "applied":
            first = _str(prov.get("executed_model"))
        elif entry.get("fixer_model") is None and "fixer_model" in entry:
            first = _str(entry.get("execution_model"))  # the retry kept the first model
    retries = [r for r in entry.get("retry_attempts") or [] if isinstance(r, dict)]
    out: list[AttemptObservation] = []
    for i, a in enumerate(raw):
        raw_n = a.get("n")
        n = raw_n if isinstance(raw_n, int) and not isinstance(raw_n, bool) else i + 1
        model = recorded.get(n)
        if model is None:
            model = first if n == 1 else (_str(retries[n - 2].get("model")) if 0 <= n - 2 < len(retries) else None)
        state = _attempt_state(a.get("verification"))
        out.append(AttemptObservation(n, model, state, _failed_tests(a.get("verification")) if state == "failed" else ()))
    return tuple(out)


def _checks(entry: dict, attempts: tuple[AttemptObservation, ...], source: str | None) -> tuple[CheckObservation, ...]:
    """Checks with a stable identity and an observed result.

    OSN runs name their verify command through ``learning.check`` (a safe
    label plus fingerprint); the per-attempt states say how often it failed.
    Other Receipts contribute canonical verification checks only when the
    whole block was observed and the check name is a safe, specific label.
    """
    out: list[CheckObservation] = []
    ident = _dict(_dict(entry.get("learning")).get("check"))
    fp, label = _str(ident.get("fingerprint")), _str(ident.get("label"))
    if fp and label and attempts:
        states = [a.state for a in attempts if a.state in ("passed", "failed")]
        if states:
            out.append(CheckObservation(
                fp, label, _str(ident.get("kind")) or "other",
                failed_attempts=sum(1 for s in states if s == "failed"),
                passed=states[-1] == "passed",
            ))
        return tuple(out)
    if source not in _OBSERVED_SOURCES:
        return ()
    for c in _dict(entry.get("verification")).get("checks") or []:
        if not isinstance(c, dict):
            continue
        name = sanitize_text(c.get("name"), 80)
        status = c.get("status")
        if not name or status not in ("passed", "failed") or name == "verify_command":
            continue
        if name.lower().startswith("bash") or "(redacted)" in name:
            continue
        digest = hashlib.sha256(f"check\x1f{name}".encode()).hexdigest()[:16]
        out.append(CheckObservation(
            digest, name, _str(c.get("kind")) or "other",
            failed_attempts=1 if status == "failed" else 0, passed=status == "passed",
        ))
    return tuple(out[:4])


def _failure_category(entry: dict, verified_success: bool | None) -> str | None:
    loop = _dict(entry.get("osn_loop"))
    if loop:
        status = _str(loop.get("status"))
        if status == "verified":
            return None
        reason = _str(loop.get("stop_reason")) or ""
        if reason.startswith("supervisor_stop:"):
            return "verification_failed"
        if status == "budget_exhausted":
            return None  # a configured limit, not a failure of the work
        return _OSN_FAILURES.get(reason)
    if verified_success is False:
        return "verification_failed"
    return None


def _policy(entry: dict) -> tuple[tuple[tuple[str, str], ...], str | None]:
    out: list[tuple[str, str]] = []
    for d in entry.get("policy_decisions") or []:
        if not isinstance(d, dict):
            continue
        decision = d.get("decision")
        if decision not in ("ask", "deny"):
            continue
        area = path_area(d.get("resource")) or "unknown_area"
        if (decision, area) not in out:
            out.append((decision, area))
    approval = _str(_dict(entry.get("approval_receipt")).get("outcome"))
    return tuple(out[:6]), approval


def observe(entry: object) -> Observation | None:
    """The learning observation for one Receipt, or None when it has no stable id.

    Never raises; never mutates *entry*.
    """
    try:
        return _observe(entry)
    except Exception:
        return None


def _observe(entry: object) -> Observation | None:
    if not isinstance(entry, dict):
        return None
    from openshard.history.verification import MODE_IMPORTED_TRANSCRIPT
    from openshard.routing.adaptive.outcome import outcome_from_receipt

    receipt_id = _str(entry.get("receipt_id"))
    if receipt_id is None:
        ts = _str(entry.get("timestamp")) or _str(entry.get("run_id"))
        if ts is None:
            return None
        receipt_id = f"run:{ts}"
    outcome = outcome_from_receipt(entry)
    verified = outcome.verified_success
    source = outcome.verification_source
    excluded: str | None = None
    if outcome.verification_observation_mode == MODE_IMPORTED_TRANSCRIPT:
        verified, excluded = None, "imported_history"
    elif verified is None:
        excluded = "agent_reported" if source == "agent_reported" else "no_observed_verification"

    attempts = _osn_attempts(entry)
    if not attempts and outcome.final_model:
        # One attempt, or a retry whose models were not recorded: attribute the
        # final outcome to the final model only when no retry happened.
        state = "passed" if verified is True else "failed" if verified is False else None
        if entry.get("retry_triggered") is not True:
            attempts = (AttemptObservation(1, outcome.final_model, state),)
    if excluded:
        # Unobserved outcomes never become per-attempt evidence either.
        attempts = tuple(AttemptObservation(a.n, a.model, None) for a in attempts)

    identity = _str(entry.get("repo_identity")) or _str(entry.get("repo_name"))
    category, category_source = task_category_for(entry)
    learning = _dict(entry.get("learning"))
    policy, approval = _policy(entry)
    used = learning.get("used") if isinstance(learning.get("used"), bool) else None
    return Observation(
        receipt_id=receipt_id,
        shard_id=_str(entry.get("shard_id")),
        timestamp=parse_timestamp(entry.get("timestamp")),
        repo=identity,
        task_category=category,
        category_source=category_source,
        harness=outcome.harness,
        terms=task_terms(entry.get("task")),
        areas=_areas(_entry_paths(entry)),
        final_model=outcome.final_model,
        verified_success=verified,
        verification_source=source,
        excluded_reason=excluded,
        attempts=attempts,
        cost_usd=outcome.cost_usd,
        duration_seconds=outcome.latency_seconds,
        retried=outcome.retry_observed,
        failure_category=_failure_category(entry, verified),
        checks=_checks(entry, attempts, source),
        policy_decisions=policy,
        approval_outcome=approval,
        learning_used=used,
        learning_signal_ids=tuple(
            s for s in learning.get("signal_ids") or [] if isinstance(s, str)
        )[:10],
    )


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


def strength_for(samples: int) -> str:
    if samples >= 5:
        return STRENGTH_STRONG
    if samples >= 3:
        return STRENGTH_MODERATE
    if samples >= 2:
        return STRENGTH_WEAK
    return STRENGTH_ANECDOTAL


def freshness_for(last_seen: datetime | None, now: datetime) -> str:
    if last_seen is None:
        return STALE  # undated evidence cannot be shown to be current
    age = (now - last_seen).days
    if age <= FRESH_DAYS:
        return FRESH
    if age <= STALE_AFTER_DAYS:
        return AGING
    return STALE


def signal_id_for(kind: str, repo: str | None, category: str | None, subject: dict) -> str:
    key = json.dumps([kind, (repo or "").lower(), category or "", subject], sort_keys=True)
    return "ls_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class LearningSignal:
    signal_id: str
    kind: str
    repo: str | None
    task_category: str | None
    subject: dict[str, Any]
    samples: int  # distinct supporting Receipts
    stats: dict[str, Any]
    strength: str
    freshness: str
    evidence_sources: tuple[str, ...]
    receipt_ids: tuple[str, ...]  # most recent first, bounded
    shard_ids: tuple[str, ...]
    first_seen: str | None
    last_seen: str | None
    terms: tuple[str, ...]
    areas: tuple[str, ...]
    summary: str
    version: int = LEARNING_SIGNAL_VERSION

    @property
    def surfaceable(self) -> bool:
        return self.strength != STRENGTH_ANECDOTAL and self.freshness != STALE

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "signal_id": self.signal_id,
            "kind": self.kind,
            "repo": self.repo,
            "task_category": self.task_category,
            "subject": dict(self.subject),
            "samples": self.samples,
            "stats": dict(self.stats),
            "strength": self.strength,
            "freshness": self.freshness,
            "evidence_sources": list(self.evidence_sources),
            "receipt_ids": list(self.receipt_ids),
            "shard_ids": list(self.shard_ids),
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "terms": list(self.terms),
            "areas": list(self.areas),
            "summary": self.summary,
        }


@dataclass
class _Acc:
    """Accumulates the Receipts behind one signal key."""

    obs: list[Observation] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, o: Observation, **counts: int) -> None:
        if all(x.receipt_id != o.receipt_id for x in self.obs):
            self.obs.append(o)
        for k, v in counts.items():
            self.counts[k] = self.counts.get(k, 0) + v


def _iso(ts: datetime | None) -> str | None:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def _model_name(model: str | None) -> str:
    if not model:
        return "unknown model"
    try:
        from openshard.history.shard_contract import display_model_name

        return display_model_name(model)
    except Exception:
        return model


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _build(kind: str, repo: str | None, category: str | None, subject: dict, acc: _Acc,
           stats: dict, summary: str, now: datetime, *, claimed: int | None = None) -> LearningSignal:
    """*claimed*: how many Receipts show the thing the summary asserts, when that is
    fewer than the Receipts observed (a check that ran 6 times but caught 2
    failures is only as strong as those 2)."""
    obs = sorted(acc.obs, key=lambda o: (o.timestamp or datetime.min.replace(tzinfo=UTC), o.receipt_id),
                 reverse=True)
    stamps = [o.timestamp for o in obs if o.timestamp]
    terms: list[str] = []
    areas: list[str] = []
    for o in obs:
        terms.extend(t for t in o.terms if t not in terms)
        areas.extend(a for a in o.areas if a not in areas)
    last = max(stamps) if stamps else None
    return LearningSignal(
        signal_id=signal_id_for(kind, repo, category, subject),
        kind=kind,
        repo=repo,
        task_category=category,
        subject=subject,
        samples=len(obs),
        stats=stats,
        strength=strength_for(len(obs) if claimed is None else min(claimed, len(obs))),
        freshness=freshness_for(last, now),
        evidence_sources=tuple(sorted({o.verification_source for o in obs if o.verification_source})),
        receipt_ids=tuple(o.receipt_id for o in obs[:MAX_REFS]),
        shard_ids=tuple(dict.fromkeys(o.shard_id for o in obs if o.shard_id))[:MAX_REFS],
        first_seen=_iso(min(stamps)) if stamps else None,
        last_seen=_iso(last),
        terms=tuple(terms[:MAX_TERMS]),
        areas=tuple(areas[:MAX_AREAS]),
        summary=summary,
    )


def _category_phrase(category: str | None) -> str:
    return f"{category} tasks" if category else "tasks"


def _model_signals(obs: list[Observation], repo: str | None, now: datetime) -> list[LearningSignal]:
    """Per (task category, first-attempt model): how the first attempt and the run ended."""
    accs: dict[tuple, _Acc] = {}
    for o in obs:
        if not o.observed or not o.attempts:
            continue
        first = o.attempts[0]
        if not first.model or first.state not in ("passed", "failed"):
            continue
        alone = all(a.model == first.model for a in o.attempts)
        accs.setdefault((o.task_category, first.model), _Acc()).add(
            o,
            first_attempt_passed=1 if first.state == "passed" else 0,
            first_attempt_failed=1 if first.state == "failed" else 0,
            run_verified=1 if o.verified_success else 0,
            needed_retry=1 if len(o.attempts) > 1 else 0,
            solo_runs=1 if alone else 0,
            solo_successes=1 if alone and o.verified_success else 0,
        )
    out = []
    for (category, model), acc in accs.items():
        c = acc.counts
        n = len(acc.obs)
        solo = [o for o in acc.obs if all(a.model == model for a in o.attempts)]
        cost = None
        if (len(solo) >= MIN_COST_SAMPLES and c.get("solo_successes")
                and all(o.cost_usd is not None for o in solo)):
            cost = round(sum(o.cost_usd or 0.0 for o in solo) / c["solo_successes"], 6)
        durations = [o.duration_seconds for o in acc.obs if o.duration_seconds is not None]
        stats = {
            "runs": n,
            "first_attempt_passed": c.get("first_attempt_passed", 0),
            "first_attempt_failed": c.get("first_attempt_failed", 0),
            "runs_verified": c.get("run_verified", 0),
            "runs_needing_retry": c.get("needed_retry", 0),
            "cost_per_verified_success_usd": cost,
            "cost_basis": "provider_reported_estimates" if cost is not None else "insufficient_cost_evidence",
            "median_duration_seconds": median(durations),
        }
        text = (
            f"{_model_name(model)} passed OpenShard-observed verification on the first attempt in "
            f"{stats['first_attempt_passed']} of {_plural(n, 'recorded run')} on {_category_phrase(category)}"
        )
        if stats["runs_needing_retry"]:
            text += f"; {stats['runs_needing_retry']} needed a retry"
        if cost is not None:
            text += f"; about ${cost:.4f} per verified success (provider estimates, runs it completed alone)"
        out.append(_build(KIND_MODEL_OUTCOMES, repo, category, {"model": model}, acc, stats, text + ".", now))
    return out


def _recovery_signals(obs: list[Observation], repo: str | None, now: datetime) -> list[LearningSignal]:
    """Per (category, failed model -> later model): how often the later attempt passed."""
    accs: dict[tuple, _Acc] = {}
    for o in obs:
        if not o.observed:
            continue
        failed_at = next((i for i, a in enumerate(o.attempts) if a.state == "failed" and a.model), None)
        if failed_at is None:
            continue
        later = [a for a in o.attempts[failed_at + 1:] if a.model and a.state in ("passed", "failed")]
        if not later:
            continue
        rescuer = next((a for a in later if a.state == "passed"), later[-1])
        key = (o.task_category, o.attempts[failed_at].model, rescuer.model)
        accs.setdefault(key, _Acc()).add(o, recovered=1 if rescuer.state == "passed" else 0)
    out = []
    for (category, from_model, to_model), acc in accs.items():
        n = len(acc.obs)
        recovered = acc.counts.get("recovered", 0)
        same = from_model == to_model
        who = f"a retry with {_model_name(to_model)}" if same else _model_name(to_model)
        text = (
            f"After {_model_name(from_model)} failed verification on {_category_phrase(category)}, "
            f"{who} passed in {recovered} of {_plural(n, 'recorded recovery attempt')}."
        )
        stats = {"attempted": n, "recovered": recovered, "not_recovered": n - recovered}
        out.append(_build(KIND_RECOVERY, repo, category, {"from_model": from_model, "to_model": to_model},
                          acc, stats, text, now))
    return out


def _check_signals(obs: list[Observation], repo: str | None, now: datetime) -> list[LearningSignal]:
    """Per (category, check): runs where the check caught a failure."""
    accs: dict[tuple, _Acc] = {}
    labels: dict[tuple, tuple[str, str]] = {}
    for o in obs:
        for c in o.checks:
            key = (o.task_category, c.fingerprint)
            labels[key] = (c.label, c.kind)
            accs.setdefault(key, _Acc()).add(
                o,
                caught=1 if c.failed_attempts else 0,
                failed_attempts=c.failed_attempts,
                caught_then_passed=1 if c.failed_attempts and c.passed else 0,
            )
    out = []
    for key, acc in accs.items():
        caught = acc.counts.get("caught", 0)
        if not caught:
            continue  # a check that never failed has taught nothing yet
        category, fingerprint = key
        label, kind = labels[key]
        n = len(acc.obs)
        fixed = acc.counts.get("caught_then_passed", 0)
        text = (
            f"`{label}` caught a failure in {caught} of {_plural(n, 'recorded run')} on "
            f"{_category_phrase(category)}"
            + (f"; {fixed} later passed after a fix" if fixed else "")
            + "."
        )
        stats = {"runs": n, "runs_caught": caught, "failed_attempts": acc.counts.get("failed_attempts", 0),
                 "caught_then_passed": fixed}
        out.append(_build(KIND_CHECK, repo, category,
                          {"check_fingerprint": fingerprint, "check_label": label, "check_kind": kind},
                          acc, stats, text, now, claimed=caught))
    return out


_TEST_WORD = re.compile(r"[a-z][a-z0-9]{2,23}")


def _test_terms(test_id: str) -> list[str]:
    """Words from a test's name, so "mobile layout" work can find ``test_mobile_viewport``."""
    name = test_id.rsplit("::", 1)[-1].lower().removeprefix("test_")
    return [w for w in _TEST_WORD.findall(name.replace("_", " ")) if w not in ("test", "tests")]


def _test_signals(obs: list[Observation], repo: str | None, now: datetime) -> list[LearningSignal]:
    """Per (category, test id): runs in which OpenShard saw that test fail."""
    accs: dict[tuple, _Acc] = {}
    for o in obs:
        failed_in: dict[str, int] = {}
        first: set[str] = set()
        for a in o.attempts:
            for t in a.failed_tests:
                failed_in[t] = failed_in.get(t, 0) + 1
                if a.n == 1:
                    first.add(t)
        for t, n in failed_in.items():
            accs.setdefault((o.task_category, t), _Acc()).add(
                o, failed_attempts=n, first_attempt=1 if t in first else 0,
                fixed_later=1 if o.verified_success else 0,
            )
    out = []
    for (category, test), acc in accs.items():
        n = len(acc.obs)
        fixed = acc.counts.get("fixed_later", 0)
        text = (
            f"`{test}` failed OpenShard-run verification in {_plural(n, 'recorded run')} on "
            f"{_category_phrase(category)}"
            + (f" ({acc.counts.get('first_attempt', 0)} on the first attempt)" if acc.counts.get("first_attempt") else "")
            + (f"; {fixed} of those runs later passed" if fixed else "")
            + "."
        )
        stats = {"runs_failed": n, "failed_attempts": acc.counts.get("failed_attempts", 0),
                 "first_attempt_failures": acc.counts.get("first_attempt", 0), "runs_fixed_later": fixed}
        signal = _build(KIND_TEST, repo, category, {"test": test}, acc, stats, text, now)
        extra = [w for w in _test_terms(test) if w not in signal.terms]
        area = path_area(test.split("::", 1)[0])
        out.append(replace(
            signal,
            terms=(*signal.terms, *extra)[:MAX_TERMS + 4],
            areas=tuple(dict.fromkeys([*( [area] if area else []), *signal.areas]))[:MAX_AREAS],
        ))
    return out


def _failure_signals(obs: list[Observation], repo: str | None, now: datetime) -> list[LearningSignal]:
    totals: dict[str | None, int] = {}
    accs: dict[tuple, _Acc] = {}
    for o in obs:
        if o.harness == "osn_loop" or o.observed:
            totals[o.task_category] = totals.get(o.task_category, 0) + 1
        if o.failure_category:
            accs.setdefault((o.task_category, o.failure_category), _Acc()).add(o)
    out = []
    for (category, failure), acc in accs.items():
        n = len(acc.obs)
        of = max(totals.get(category, n), n)
        text = (
            f"{n} of {_plural(of, 'recorded run')} on {_category_phrase(category)} "
            f"{FAILURE_LABELS.get(failure, failure)}."
        )
        stats = {"runs": n, "comparable_runs": of,
                 "operational": failure in OPERATIONAL_FAILURES}
        out.append(_build(KIND_FAILURE, repo, category, {"failure_category": failure}, acc, stats, text, now))
    return out


def _policy_signals(obs: list[Observation], repo: str | None, now: datetime) -> list[LearningSignal]:
    accs: dict[tuple, _Acc] = {}
    for o in obs:
        for decision, area in o.policy_decisions:
            accs.setdefault((o.task_category, decision, area), _Acc()).add(
                o,
                approval_granted=1 if o.approval_outcome == "granted" else 0,
                approval_refused=1 if o.approval_outcome in ("refused", "unanswered", "approver_error") else 0,
            )
    out = []
    for (category, decision, area), acc in accs.items():
        n = len(acc.obs)
        verb = "required approval" if decision == "ask" else "were denied by policy"
        where = "the repository root" if area == "." else f"`{area}/`"
        text = f"Writes under {where} {verb} in {_plural(n, 'recorded run')} on {_category_phrase(category)}."
        stats = {"runs": n, "approval_granted": acc.counts.get("approval_granted", 0),
                 "approval_not_granted": acc.counts.get("approval_refused", 0)}
        out.append(_build(KIND_POLICY, repo, category, {"decision": decision, "area": area}, acc, stats, text, now))
    return out


def median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return round(s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2, 3)


@dataclass(frozen=True)
class LearningIndex:
    """Every signal derived for one repository, plus what was read to derive them."""

    repo: str | None
    signals: tuple[LearningSignal, ...]
    entries_scanned: int
    receipts_observed: int  # distinct Receipts with a stable id
    receipts_with_evidence: int  # ... whose outcome was observed
    excluded: dict[str, int]
    unreadable: int = 0
    observations: tuple[Observation, ...] = ()

    def get(self, signal_id: str) -> LearningSignal | None:
        return next((s for s in self.signals if s.signal_id == signal_id), None)


def _sort_key(s: LearningSignal) -> tuple:
    return (KINDS.index(s.kind), s.task_category or "", -s.samples, s.signal_id)


def derive_signals(
    entries: Iterable[object],
    *,
    repo: str | None = None,
    now: datetime | None = None,
    unreadable: int = 0,
) -> LearningIndex:
    """Derive every learning signal from *entries* (oldest first). Pure; never raises.

    With *repo*, only entries recorded for that repository contribute (see
    ``repo_identity.entry_matches_repo``); an entry with no repository marker
    at all is not assumed to belong to it.
    """
    from openshard.history.repo_identity import entry_matches_repo

    now = now or datetime.now(UTC)
    by_id: dict[str, Observation] = {}
    scanned = 0
    for entry in entries:
        scanned += 1
        if not isinstance(entry, dict):
            unreadable += 1
            continue
        if repo and not entry_matches_repo(entry, repo):
            continue
        o = observe(entry)
        if o is None:
            continue
        by_id[o.receipt_id] = o  # a re-recorded Receipt replaces its earlier copy
    obs = list(by_id.values())
    excluded: dict[str, int] = {}
    for o in obs:
        if o.excluded_reason:
            excluded[o.excluded_reason] = excluded.get(o.excluded_reason, 0) + 1
    signals: list[LearningSignal] = []
    for builder in (_model_signals, _recovery_signals, _check_signals, _test_signals, _failure_signals,
                    _policy_signals):
        try:
            signals.extend(builder(obs, repo, now))
        except Exception:
            continue  # one malformed family never hides the others
    signals.sort(key=_sort_key)
    return LearningIndex(
        repo=repo,
        signals=tuple(signals),
        entries_scanned=scanned,
        receipts_observed=len(obs),
        receipts_with_evidence=sum(1 for o in obs if o.observed),
        excluded=excluded,
        unreadable=unreadable,
        observations=tuple(obs),
    )


def read_entries(runs_path: Path) -> tuple[list[dict], int]:
    """(entries, unreadable line count) from a ``runs.jsonl``, most recent
    ``MAX_ENTRIES_SCANNED`` only. Never raises: a missing file is no history."""
    entries: list[dict] = []
    bad = 0
    try:
        with runs_path.open(encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return [], 0
    for line in lines[-MAX_ENTRIES_SCANNED:]:
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(value, dict):
            entries.append(value)
        else:
            bad += 1
    return entries, bad


def repo_key(repo_root: Path) -> str:
    """The identity this checkout's Receipts carry: the canonical remote, else the folder name."""
    try:
        from openshard.history.repo_identity import capture_repo_identity

        ident = capture_repo_identity(repo_root)
    except Exception:
        ident = None
    return ident or repo_root.name


def load_learning_index(repo_root: Path, *, repo: str | None = None, now: datetime | None = None) -> LearningIndex:
    """Derive signals from ``<repo_root>/.openshard/runs.jsonl`` for this repository."""
    entries, bad = read_entries(repo_root / ".openshard" / "runs.jsonl")
    return derive_signals(entries, repo=repo or repo_key(repo_root), now=now, unreadable=bad)


__all__ = [
    "KINDS",
    "KIND_CHECK",
    "KIND_FAILURE",
    "KIND_MODEL_OUTCOMES",
    "KIND_POLICY",
    "KIND_RECOVERY",
    "KIND_TEST",
    "LEARNING_SIGNAL_VERSION",
    "LearningIndex",
    "LearningSignal",
    "Observation",
    "derive_signals",
    "freshness_for",
    "load_learning_index",
    "observe",
    "read_entries",
    "repo_key",
    "strength_for",
    "task_category_for",
    "task_terms",
]
