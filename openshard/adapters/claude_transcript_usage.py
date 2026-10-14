"""Token usage from Claude Code session transcripts.

Claude Code appends every API message of a session, with the provider's
``usage`` counters, to ``~/.claude/projects/<project>/<session>.jsonl`` (and
each subagent's to a ``subagents/`` folder beside it). This module reads those
counters: de-duplicated per message id across files (a streamed message is
re-reported line by line; the last line wins), limited to messages stamped at
or after a receipt's window, with completeness withheld when a counter is
malformed or the file cap was hit. It never stores transcript content or
paths; the fold (``claude_hooks._transcript_usage``) owns the incremental
state on the session buffer and calls ``_read_transcript_increment`` with it.

Moved out of ``claude_hooks`` unchanged; that module re-exports these names.
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from openshard.adapters.capture_agents import AGENT_CLAUDE_CODE, AGENT_CODEX

_TRANSCRIPT_MAX_FILES = 50  # main transcript + subagent transcripts read per receipt
_TRANSCRIPT_RECENT_IDS = 16  # message ids per file kept open for re-reported streaming lines
_TRANSCRIPT_MAX_CLOSED_IDS = 20_000  # closed message ids remembered for de-duplication across files
_MAX_USAGE_MODELS = 10
# One message's usage: input, output, cache_read, cache_creation (total),
# cache_creation 5m, cache_creation 1h, cache_creation with no TTL split.
_USAGE_FIELDS = ("input", "output", "cache_read", "cache_creation",
                 "cache_creation_5m", "cache_creation_1h", "cache_creation_unsplit")


def _transcript_roots() -> list[Path]:
    """Where Claude Code keeps transcripts: ``~/.claude/projects`` and ``$CLAUDE_CONFIG_DIR/projects``."""
    roots = [Path.home() / ".claude" / "projects"]
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if isinstance(config_dir, str) and config_dir.strip():
        roots.append(Path(config_dir.strip()) / "projects")
    return roots


def _valid_transcript_path(
    raw: object,
    session_id: str | None,
    *,
    agent: str = AGENT_CLAUDE_CODE,
) -> str | None:
    """Return a validated transient transcript locator for this agent/session.

    Claude Code has a stable projects-directory layout. Codex exposes a
    ``transcript_path`` hook field whose hosted/local location can differ, so
    its validator checks the Codex-owned path shape and the reader later
    proves the embedded session id before trusting usage.
    """
    if agent == AGENT_CODEX:
        from openshard.adapters.codex_transcript import valid_codex_transcript_path

        return valid_codex_transcript_path(raw, session_id)
    if not isinstance(raw, str) or not raw or len(raw) > 2_000 or not isinstance(session_id, str) or not session_id:
        return None
    if raw.startswith(("\\\\", "//")):
        return None
    try:
        path = Path(raw)
        if not path.is_absolute() or path.name != f"{session_id}.jsonl":
            return None
        resolved = path.resolve()
        for root in _transcript_roots():
            try:
                if resolved.is_relative_to(root.resolve()):
                    return raw
            except (OSError, ValueError):
                continue
    except (ValueError, OSError):
        return None
    return None


def _transcript_files(path: Path) -> tuple[list[Path], bool]:
    """The session transcript plus its subagent transcripts; True when some were left out (cap)."""
    files = [path]
    capped = False
    try:
        sub = path.with_suffix("") / "subagents"
        if sub.is_dir():
            children = sorted(p for p in sub.glob("*.jsonl") if p.is_file())
            capped = len(children) > _TRANSCRIPT_MAX_FILES - 1
            files.extend(children[: _TRANSCRIPT_MAX_FILES - 1])
    except OSError:
        pass
    return files, capped


def _parse_utc(stamp: object) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _usage_row(usage: Mapping[str, Any]) -> list[int]:
    """One API message's usage as ``_USAGE_FIELDS`` (the 5m/1h cache-write split when reported)."""
    creation = _count(usage.get("cache_creation_input_tokens"))
    split = usage.get("cache_creation")
    five = one = 0
    unsplit = creation
    if isinstance(split, Mapping):
        five = _count(split.get("ephemeral_5m_input_tokens"))
        one = _count(split.get("ephemeral_1h_input_tokens"))
        unsplit = max(0, creation - five - one)
    return [_count(usage.get("input_tokens")), _count(usage.get("output_tokens")),
            _count(usage.get("cache_read_input_tokens")), creation, five, one, unsplit]


def _new_transcript_state(since: object) -> dict:
    return {"since": since, "files": {}, "closed": {}, "closed_ids": [], "capped": False}


def _file_head(file: Path) -> str | None:
    """A short hash of the file's first bytes: a replaced transcript (even a longer one) changes it."""
    import hashlib

    try:
        with file.open("rb") as fh:
            return hashlib.sha256(fh.read(512)).hexdigest()[:16]
    except OSError:
        return None


def _long_context(model: str, row: list[int]) -> bool:
    """The tier of one validated message, including cached prompt tokens."""
    from openshard.models.pricing import official_rate

    rate = official_rate(model)
    return bool(rate and rate.long_context_threshold is not None
                and row[0] + row[2] + row[3] > rate.long_context_threshold)


def _close_message(state: dict, mid: str, model: str, row: list) -> None:
    """Fold one message whose streaming lines are behind us into its model's totals, once per id."""
    closed_ids: list = state.setdefault("closed_ids", [])
    if mid in closed_ids:
        return
    if len(closed_ids) >= _TRANSCRIPT_MAX_CLOSED_IDS:
        state["capped"] = True  # de-duplication memory exhausted: totals no longer claimed complete
    else:
        closed_ids.append(mid)
    closed = state.setdefault("closed", {})
    totals = closed.get(model)
    if not isinstance(totals, list) or len(totals) != len(_USAGE_FIELDS) + 1:
        totals = [0] * (len(_USAGE_FIELDS) + 1)
    for i, v in enumerate(row):
        totals[i] += int(v)
    totals[-1] += 1  # messages
    closed[model] = totals
    if _long_context(model, row):
        long = state.setdefault("long_context_closed", {}).setdefault(model, [0] * (len(_USAGE_FIELDS) + 1))
        for i, v in enumerate(row):
            long[i] += int(v)
        long[-1] += 1


def _read_transcript_increment(state: dict, files: list[Path]) -> bool:
    """Read what was appended to *files* since *state*'s offsets. False when the main file is unreadable.

    A file that shrank or was replaced (its first bytes changed) invalidates
    every offset: the caller starts over. Only complete lines are consumed.
    A message id is counted once across *all* files: each file keeps its
    last few ids open (a streamed message's lines are contiguous and may
    straddle two reads, so a continuing id replaces rather than adds), and
    an id already closed or open elsewhere is skipped. Never raises.
    """
    from openshard.adapters.claude_code_import import _sanitize_model

    since = _parse_utc(state.get("since"))
    closed_ids = set(state.get("closed_ids") or [])
    for index, file in enumerate(files):
        key = "main" if index == 0 else file.name
        slot = state["files"].setdefault(key, {"offset": 0, "recent": {}})
        try:
            size = file.stat().st_size
            if size < int(slot.get("offset") or 0):
                return False  # truncated: the caller resets the state
            head = _file_head(file)
            if int(slot.get("offset") or 0) and slot.get("head") and head != slot.get("head"):
                return False  # replaced by another file: the caller resets the state
            slot["head"] = head
            with file.open("rb") as fh:
                fh.seek(int(slot.get("offset") or 0))
                chunk = fh.read(size - int(slot.get("offset") or 0))
        except OSError:
            if index == 0:
                return False
            continue
        end = chunk.rfind(b"\n")
        if end < 0:
            continue
        slot["offset"] = int(slot.get("offset") or 0) + end + 1
        recent: dict = slot.setdefault("recent", {})
        for raw in chunk[: end + 1].splitlines():
            if b'"usage"' not in raw or b'"assistant"' not in raw:
                continue
            try:
                rec = json.loads(raw)
            except (ValueError, RecursionError):
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            mid, usage = msg.get("id"), msg.get("usage")
            if not isinstance(mid, str) or not mid or not isinstance(usage, dict):
                continue
            if since is not None:
                at = _parse_utc(rec.get("timestamp"))
                if at is None or at < since:
                    continue
            model = msg.get("model")
            name = _sanitize_model(model) if isinstance(model, str) and model != "<synthetic>" else "unknown"
            if mid not in recent and (mid in closed_ids or any(
                mid in (other or {}).get("recent", {}) for k, other in state["files"].items() if k != key
            )):
                continue  # already counted (earlier in this file, or in another transcript)
            # Missing or malformed counters are not zero usage. Keep any older
            # observed lower bound, but withhold completeness and pricing until
            # a valid later record for this exact message resolves it.
            invalid = state.setdefault("invalid_usage", {})
            fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            if any(not isinstance(usage.get(f), int) or isinstance(usage.get(f), bool) or usage[f] < 0 for f in fields):
                invalid[mid] = True
                continue
            invalid.pop(mid, None)
            order = state.setdefault("model_order", [])
            if name not in order:
                order.append(name)
            recent.pop(mid, None)  # the latest line of a streamed message wins
            recent[mid] = [name, *_usage_row(usage)]
            while len(recent) > _TRANSCRIPT_RECENT_IDS:
                old_id = next(iter(recent))
                old = recent.pop(old_id)
                _close_message(state, old_id, old[0], old[1:])
                closed_ids.add(old_id)
    return True


def _summarise_transcript_state(state: dict) -> dict | None:
    """``{"messages", "totals", "by_model", "complete"}`` from an incremental state, or None when empty."""
    per_model: dict[str, list[int]] = {}
    long_context = {m: list(acc) for m, acc in (state.get("long_context_closed") or {}).items()}
    for model, totals in (state.get("closed") or {}).items():
        if isinstance(totals, list) and len(totals) == len(_USAGE_FIELDS) + 1:
            per_model[model] = [int(v) for v in totals]
    for slot in (state.get("files") or {}).values():
        for row in ((slot or {}).get("recent") or {}).values():
            if not isinstance(row, list) or len(row) != len(_USAGE_FIELDS) + 1:
                continue
            acc = per_model.setdefault(row[0], [0] * (len(_USAGE_FIELDS) + 1))
            for i, v in enumerate(row[1:]):
                acc[i] += int(v)
            acc[-1] += 1
            if _long_context(row[0], row[1:]):
                long = long_context.setdefault(row[0], [0] * (len(_USAGE_FIELDS) + 1))
                for i, v in enumerate(row[1:]):
                    long[i] += int(v)
                long[-1] += 1
    messages = sum(acc[-1] for acc in per_model.values())
    if not messages:
        return None
    totals = {f: sum(acc[i] for acc in per_model.values()) for i, f in enumerate(_USAGE_FIELDS)}
    by_model = {
        m: {**{f: acc[i] for i, f in enumerate(_USAGE_FIELDS)}, "messages": acc[-1]}
        for m in dict.fromkeys([*(state.get("model_order") or []), *per_model])
        if m in per_model
        for acc in [per_model[m]]
    }
    from openshard.models.pricing import official_rate

    for model in by_model:
        rate = official_rate(model)
        if rate and rate.long_context_threshold is not None:
            long_context.setdefault(model, [0] * (len(_USAGE_FIELDS) + 1))
    for model, acc in long_context.items():
        by_model[model]["long_context"] = {
            **{f: acc[i] for i, f in enumerate(_USAGE_FIELDS)}, "messages": acc[-1],
        }
    complete = not state.get("capped") and not state.get("invalid_usage") and len(by_model) <= _MAX_USAGE_MODELS
    return {"messages": messages, "totals": totals, "by_model": dict(list(by_model.items())[:_MAX_USAGE_MODELS]),
            "complete": complete,
            **({"incomplete_reason": "malformed_runtime_counter"} if state.get("invalid_usage") else {})}


def read_transcript_usage(path: Path, *, since: datetime | str | None = None) -> dict | None:
    """Token usage summed from a Claude Code transcript (one full read), or None. Never raises.

    De-duplicated per message id, limited to assistant records stamped at or
    after *since*, subagent transcripts included. See ``_transcript_usage``
    for the incremental form the fold uses.
    """
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(since, datetime) else since
    state = _new_transcript_state(stamp)
    files, capped = _transcript_files(path)
    state["capped"] = capped
    if not _read_transcript_increment(state, files):
        return None
    return _summarise_transcript_state(state)
