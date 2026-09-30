"""The Receipt ``learning`` block: what learning a run consulted and what came of it.

Compact by design. It names the signals used (ids, kind, strength, sample
size, why each was selected), whether routing and verification were
influenced, and two observations that let *later* runs learn from this one:
the model each attempt requested and a privacy-safe identity for the verify
command. Signal summaries and supporting evidence are not copied in; they are
re-derivable from history with ``openshard learn inspect <signal_id>``.

"Influenced" is claimed only when it happened:

* ``context_supplied`` is True when the advisory block was actually put in
  front of the model (a model call was made with it);
* ``routing.influenced`` is True only when Adaptive Routing V2 applied its
  decision *and* that decision used history evidence. Otherwise history did
  not choose the model, whatever the advisory text said;
* ``verification.influenced`` is always False in V1: recommended checks are
  shown, never executed. ``current_check_recommended`` says whether the check
  the user supplied is the one history points at.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from openshard.learning.retrieval import LearningContext
from openshard.safety.sanitize import is_absolute_path, looks_like_secret

LEARNING_RECORD_VERSION = 1
MAX_LABEL = 120

_SAFE_TOKEN = re.compile(r"^[\w.:/@%+,=-]{1,60}$")
# A flag or assignment whose name suggests a credential: its value is never shown.
_SENSITIVE = re.compile(r"(?i)(pass|pwd|token|secret|key|auth|cred|cookie|session|bearer|signature)")


def _exe_name(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    lower = name.lower()
    if lower.endswith(".exe"):
        name, lower = name[:-4], lower[:-4]
    if re.match(r"^python\d*(\.\d+)?$", lower):
        return "python"
    return name


def _safe_arg(token: str, previous: str | None) -> bool:
    if not _SAFE_TOKEN.match(token) or is_absolute_path(token) or looks_like_secret(token):
        return False
    if previous is not None and previous.startswith("-") and _SENSITIVE.search(previous):
        return False  # the value after --password / --api-key / -t token ...
    if "=" in token and _SENSITIVE.search(token.split("=", 1)[0]):
        return False  # --token=..., DB_PASS=...
    return ".." not in token.replace("\\", "/").split("/")


def check_identity(argv: list[str] | tuple[str, ...]) -> dict[str, Any] | None:
    """``{"fingerprint", "label", "label_complete", "kind"}`` for a verify command.

    The fingerprint is a hash of the command with the executable reduced to its
    name, so the same check run from different interpreters or machines
    matches. The label shows the command only when every argument is a plain
    token (no quotes, spaces, shell syntax, absolute paths or secret-like
    values); otherwise it shows the executable and withholds the arguments.
    """
    if not argv:
        return None
    exe = _exe_name(str(argv[0]))
    args = [str(a) for a in argv[1:]]
    digest = hashlib.sha256("\x1f".join([exe, *args]).encode("utf-8", "replace")).hexdigest()[:16]
    safe = all(_safe_arg(a, args[i - 1] if i else None) for i, a in enumerate(args))         and _SAFE_TOKEN.match(exe) is not None
    label = " ".join([exe, *args]) if safe else f"{exe} …"
    if len(label) > MAX_LABEL:
        label, safe = label[: MAX_LABEL - 2] + " …", False
    return {"fingerprint": digest, "label": label, "label_complete": safe, "kind": _check_kind(exe, args)}


def _check_kind(exe: str, args: list[str]) -> str:
    text = " ".join([exe, *args]).lower()
    if any(k in text for k in ("pytest", "jest", "vitest", "test", "mocha", "playwright", "cypress")):
        return "test"
    if any(k in text for k in ("ruff", "eslint", "lint", "flake8", "pylint")):
        return "lint"
    if any(k in text for k in ("mypy", "tsc", "pyright", "typecheck")):
        return "typecheck"
    if any(k in text for k in ("build", "compile", "py_compile")):
        return "build"
    return "other"


def build_learning_record(
    ctx: LearningContext | None,
    *,
    check: dict[str, Any] | None,
    attempt_models: list[tuple[int, str]] | None = None,
    context_supplied: bool = False,
    routing_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``learning`` block for one OSN Receipt. Never raises on odd input."""
    record: dict[str, Any] = {"version": LEARNING_RECORD_VERSION}
    if ctx is None:
        record.update({"consulted": False, "status": "disabled", "used": False})
    else:
        record.update({
            "consulted": ctx.status not in ("disabled",),
            "status": ctx.status,
            "used": ctx.used,
            "signals_considered": ctx.signals_considered,
            "signals_used": len(ctx.retrieved) if ctx.used else 0,
            "signal_ids": [r.signal.signal_id for r in ctx.retrieved] if ctx.used else [],
            "signals": [r.to_record() for r in ctx.retrieved] if ctx.used else [],
            "supporting_receipt_ids": ctx.supporting_receipt_ids if ctx.used else [],
            "task_shape": ctx.shape.to_dict() if ctx.shape else None,
        })
        if ctx.error:
            record["error"] = ctx.error
    record["context_supplied"] = bool(context_supplied and record.get("used"))
    record["routing"] = routing_influence(routing_record)
    recs = ctx.recommended_checks if ctx is not None and ctx.used else []
    record["verification"] = {
        "influenced": False,
        "mode": "advisory_only",
        "recommended_checks": [
            {"signal_id": r.signal_id, "label": r.label, "fingerprint": r.fingerprint,
             "runs_caught": r.runs_caught, "runs": r.runs}
            for r in recs[:3]
        ],
        "current_check_recommended": bool(ctx is not None and ctx.current_check_recommended),
    }
    if check:
        record["check"] = dict(check)
    if attempt_models:
        record["attempt_models"] = [{"attempt": n, "model": m} for n, m in attempt_models[:5]]
    return record


def routing_influence(routing_record: dict[str, Any] | None) -> dict[str, Any]:
    """Whether recorded history chose the model, read from the ``adaptive_routing`` block."""
    if not isinstance(routing_record, dict):
        return {"influenced": False, "reason": "adaptive_routing_not_governing"}
    raw = routing_record.get("history")
    history: dict[str, Any] = raw if isinstance(raw, dict) else {}
    applied = routing_record.get("applied") is True
    used = history.get("used") is True
    out: dict[str, Any] = {
        "influenced": applied and used,
        "reason": (
            "history_evidence_used" if applied and used
            else "routing_not_applied" if not applied
            else str(history.get("reason") or "history_not_used")
        ),
    }
    if history.get("scope"):
        out["history_scope"] = history.get("scope")
    return out


__all__ = [
    "LEARNING_RECORD_VERSION",
    "build_learning_record",
    "check_identity",
    "routing_influence",
]
