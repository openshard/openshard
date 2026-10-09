"""Live Codex transcript usage for hook-captured Receipts.

Codex hooks expose ``transcript_path`` but not token counts directly. The
runtime transcript contains cumulative ``token_count`` records. This module
reads only the identity, model/provider identifiers and numeric usage fields
needed for a Receipt. Prompt/assistant/tool content is never retained.

The transcript format is explicitly not a stable hook API, so parsing is
fail-closed: an unknown/malformed shape, a mismatched session id or an
oversized file produces no usage rather than a guess.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_MAX_BYTES = 64 * 1024 * 1024
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:/@()+-]{0,199}$")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,79}$")


def _codex_home(env: Mapping[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    configured = source.get("CODEX_HOME")
    return Path(configured) if isinstance(configured, str) and configured else Path.home() / ".codex"


def valid_codex_transcript_path(
    raw: object,
    session_id: str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return an absolute Codex transcript path that is safe to inspect.

    Local Codex normally writes under ``$CODEX_HOME``/``~/.codex``. Hosted
    Codex can expose a workspace-local ``.codex/rollout.jsonl`` path, so that
    documented shape is accepted too. The file still has to prove its own
    session id before any usage is trusted.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 2_000 or not isinstance(session_id, str) or not session_id:
        return None
    if raw.startswith(("\\\\", "//")):
        return None
    try:
        path = Path(raw)
        if not path.is_absolute() or path.suffix.lower() != ".jsonl":
            return None
        resolved = path.resolve()
        try:
            if resolved.is_relative_to(_codex_home(env).resolve()):
                return raw
        except (OSError, ValueError):
            pass
        if ".codex" in resolved.parts:
            return raw
    except (OSError, ValueError):
        return None
    return None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _safe_identifier(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.match(value) else None


def read_codex_transcript_usage(path: Path, session_id: str) -> dict[str, Any] | None:
    """Read the latest cumulative token total for *session_id*. Never raises."""
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
    except OSError:
        return None

    session_seen = False
    models: list[str] = []
    provider: str | None = None
    latest: dict[str, int] | None = None
    complete = True

    try:
        with path.open("rb") as stream:
            for raw in stream:
                if len(raw) > 2_000_000:
                    return None
                if b'"type"' not in raw:
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict):
                    continue
                kind = record.get("type")
                if kind == "session_meta" and not session_seen:
                    sid = payload.get("id")
                    if not isinstance(sid, str) or sid != session_id:
                        return None
                    session_seen = True
                    provider = _safe_identifier(payload.get("model_provider"), _PROVIDER_RE)
                elif kind == "turn_context":
                    model = _safe_identifier(payload.get("model"), _MODEL_RE)
                    if model and model not in models:
                        models.append(model)
                elif kind == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(total, dict):
                        continue
                    input_total = _count(total.get("input_tokens"))
                    output = _count(total.get("output_tokens"))
                    cached = _count(total.get("cached_input_tokens"))
                    if input_total is None or output is None or cached is None or cached > input_total:
                        complete = False
                        continue
                    latest = {
                        "input": input_total - cached,
                        "output": output,
                        "cache_read": cached,
                        "cache_creation": 0,
                        "cache_creation_5m": 0,
                        "cache_creation_1h": 0,
                        "cache_creation_unsplit": 0,
                    }
    except (OSError, ValueError):
        return None

    if not session_seen or latest is None:
        return None

    # A cumulative token counter can be priced only when one model served the
    # session. Tokens remain useful when the model changed, but cost stays
    # unknown because the aggregate cannot be apportioned honestly.
    model = models[0] if len(models) == 1 else "unknown"
    row = {**latest, "messages": 1}
    return {
        "messages": 1,
        "totals": dict(latest),
        "by_model": {model: row},
        "complete": complete,
        **({"incomplete_reason": "malformed_runtime_counter"} if not complete else {}),
        "provider": provider,
        "models": models[:5],
    }


__all__ = ["read_codex_transcript_usage", "valid_codex_transcript_path"]
