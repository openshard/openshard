"""The scrub boundary: ``ParsedSession`` (transient, raw) -> ``HistoricalSession``.

Nothing raw crosses this function. Every string is secret-scrubbed with the
same scrubbers the live capture path uses (``scrub_text_for_secrets``,
``sanitize_text`` / ``sanitize_path``, the hook command reducer, the task
title derivation); every path is made repo-relative, and a path outside the
repository is dropped (not even its basename is kept); absolute and home
paths left inside free text are masked. Prompt text is kept only as the
bounded first-prompt excerpt the hook path also keeps. Tool output is never
kept -- only the commit SHAs git printed in it.
"""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from openshard.history.event import (
    EVIDENCE_AGENT_REPORTED,
    EVIDENCE_IMPORTED_TRANSCRIPT,
)
from openshard.ingest.model import (
    OUTCOME_PASSED,
    Fact,
    HistoricalCommand,
    HistoricalFileEdit,
    HistoricalSession,
    ParsedSession,
    unknown_fact,
)

TASK_CAP = 300
MESSAGE_CAP = 300
PATH_CAP = 200
MAX_COMMANDS = 200
MAX_FILES = 200
MAX_TOOL_EVENTS = 200

LOSS_PATH_OUTSIDE_REPO = "path_outside_repo"

_WIN_ABS_RE = re.compile(r"\b[A-Za-z]:[\\/][^\s\"'`<>|]*")
# Also matches Git Bash / MSYS drive paths (``/c/Users/...``).
_POSIX_ABS_RE = re.compile(
    r"(?<![\w.~])(?:/[A-Za-z])?/(?:Users|home|root|private|var|tmp|mnt|opt|etc|Volumes)/[^\s\"'`<>|]*",
    re.IGNORECASE,
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def import_key(agent: str, native_session_id: str) -> str:
    return f"{agent}:{native_session_id}"


def _path_variants(path: str) -> list[str]:
    """*path* spelled with backslashes, forward slashes and as Git Bash ``/c/...``; longest first."""
    fwd = path.replace("\\", "/").rstrip("/")
    out = {fwd, fwd.replace("/", "\\")}
    if len(fwd) > 2 and fwd[1] == ":":
        out.add(f"/{fwd[0].lower()}{fwd[2:]}")
    return sorted((v for v in out if len(v) > 3), key=len, reverse=True)


def _mask_paths(text: str, repo_root: Path | None) -> str:
    """Relativize the repo root, then mask any remaining absolute path."""
    if repo_root is not None:
        for variant in _path_variants(str(repo_root)):
            text = re.sub(re.escape(variant) + r"(?![\w.-])[\\/]?", "./", text, flags=re.IGNORECASE)
    try:
        home = str(Path.home())
    except (OSError, RuntimeError):
        home = ""
    for variant in _path_variants(home) if home else []:
        text = re.sub(re.escape(variant) + r"(?![\w.-])", "~", text, flags=re.IGNORECASE)
    text = _WIN_ABS_RE.sub("<path>", text)
    return _POSIX_ABS_RE.sub("<path>", text)


def scrub_free_text(text: object, cap: int, repo_root: Path | None) -> str | None:
    """Secret-scrubbed, path-masked, whitespace-collapsed, capped text, or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    from openshard.security.secret_scan import scrub_text_for_secrets

    scrubbed, _ = scrub_text_for_secrets(text[: cap * 8], source_label="<historical-import>")
    masked = _mask_paths(scrubbed, repo_root)
    printable = "".join(ch if ch.isprintable() else " " for ch in masked)
    collapsed = " ".join(printable.split())
    return collapsed[:cap] or None


def scrub_label(value: object, cap: int) -> str | None:
    from openshard.safety.sanitize import sanitize_text

    return sanitize_text(value, cap)


def repo_relative(raw_path: object, repo_root: Path, base: str | None = None) -> str | None:
    """*raw_path* relative to *repo_root* (posix), or None when outside it.

    A relative path is anchored at *base* (the tool call's working directory)
    when given, else at the repo root. Never touches the filesystem beyond
    resolving the root itself.
    """
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    from openshard.safety.sanitize import sanitize_path

    raw = raw_path.strip()
    flavour = PureWindowsPath if (PureWindowsPath(raw).is_absolute() or "\\" in raw) else PurePosixPath
    candidate = flavour(raw)
    if not candidate.is_absolute():
        anchor = base if isinstance(base, str) and base else str(repo_root)
        a_flavour = PureWindowsPath if (PureWindowsPath(anchor).is_absolute() or "\\" in anchor) else PurePosixPath
        candidate = a_flavour(anchor) / raw
        flavour = a_flavour
    root = flavour(str(repo_root))
    parts = list(candidate.parts)
    # Collapse ".." without touching the filesystem.
    norm: list[str] = []
    for part in parts:
        if part == "..":
            if len(norm) > 1:
                norm.pop()
            continue
        if part != ".":
            norm.append(part)
    candidate = flavour(*norm) if norm else candidate
    if flavour is PureWindowsPath:
        cand_l = [p.lower() for p in candidate.parts]
        root_l = [p.lower() for p in root.parts]
        if cand_l[: len(root_l)] != root_l:
            return None
        rel_parts = candidate.parts[len(root.parts):]
    else:
        try:
            rel_parts = candidate.relative_to(root).parts
        except ValueError:
            return None
    if not rel_parts:
        return None
    return sanitize_path("/".join(rel_parts), PATH_CAP)


def _fact(value, evidence: str, ref: str | None) -> Fact:
    return Fact(value, evidence, ref) if value not in (None, "", [], {}) else unknown_fact(ref)


def normalize(parsed: ParsedSession, repo_root: Path, source_sha256: str) -> HistoricalSession:
    """Reduce *parsed* to scrubbed, provenance-labelled facts. Never keeps raw text."""
    from openshard.adapters.claude_code_import import _sanitize_model
    from openshard.adapters.claude_hooks import summarize_command
    from openshard.history.repo_identity import canonicalize_remote_url
    from openshard.history.task_title import normalize_title_candidate

    T = EVIDENCE_IMPORTED_TRANSCRIPT
    hs = HistoricalSession(
        parser=parsed.parser,
        agent=parsed.agent,
        native_session_id=parsed.native_session_id,
        import_key=import_key(parsed.agent, parsed.native_session_id),
        source_sha256=source_sha256,
        repo_root=str(repo_root),
        losses=dict(parsed.losses),
    )
    f = hs.facts
    sid = parsed.native_session_id if _SESSION_ID_RE.match(parsed.native_session_id or "") else None
    f["session_id"] = _fact(sid, T, parsed.session_ref)
    window = {"start": parsed.start, "end": parsed.end} if parsed.start else None
    f["window"] = _fact(window, T, parsed.window_ref)
    cwd_rel = repo_relative(parsed.cwd, repo_root) if parsed.cwd else None
    if parsed.cwd and cwd_rel is None:
        cwd_rel = "." if _same_dir(parsed.cwd, repo_root) else None
    f["cwd"] = _fact(cwd_rel, T, parsed.cwd_ref)
    f["branch"] = _fact(scrub_label(parsed.branch, 120), T, parsed.branch_ref)
    f["head_at_start"] = _fact(parsed.head_at_start, T, parsed.head_ref)
    f["repo"] = _fact(canonicalize_remote_url(parsed.repository_url), T, parsed.repository_ref)
    f["task"] = _fact(scrub_free_text(parsed.task, TASK_CAP, repo_root), T, parsed.task_ref)
    f["title"] = _fact(normalize_title_candidate(parsed.title) if parsed.title else None,
                       EVIDENCE_AGENT_REPORTED, parsed.title_ref)
    models = [m for m in (_sanitize_model(m) for m in parsed.models) if m and m != "unknown"]
    f["model"] = _fact(models[0] if models else None, T, parsed.model_ref)
    f["models"] = _fact(models, T, parsed.model_ref)
    f["provider"] = _fact(scrub_label(parsed.provider, 60), T, parsed.provider_ref)
    f["agent_version"] = _fact(scrub_label(parsed.agent_version, 40), T, parsed.session_ref)
    f["tokens"] = _fact(dict(parsed.tokens), T, parsed.tokens_ref)
    f["approval_policy"] = _fact(scrub_label(parsed.approval_policy, 60), T, parsed.approval_ref)
    # Neither Claude Code nor Codex history records individual approval decisions.
    f["approvals"] = unknown_fact()
    f["cost"] = unknown_fact()  # historical cost is not produced in v1 (see §15.5)
    f["final_message"] = _fact(scrub_free_text(parsed.final_message, MESSAGE_CAP, repo_root),
                               EVIDENCE_AGENT_REPORTED, parsed.final_message_ref)
    f["pr"] = _fact(scrub_label(parsed.pr_url, 200), T, parsed.pr_ref)
    f["turns"] = _fact(parsed.turns or None, T, parsed.window_ref)

    session_cwd = parsed.cwd
    edits: dict[str, HistoricalFileEdit] = {}
    for call in parsed.tool_calls:
        tool = scrub_label(call.name, 60) or "tool"
        hs.tool_counts[tool] = hs.tool_counts.get(tool, 0) + 1
        command_index: int | None = None
        if call.command is not None and len(hs.commands) < MAX_COMMANDS:
            action, _target, kind = summarize_command(_mask_paths(call.command, repo_root), label=tool)
            command_index = len(hs.commands)
            hs.commands.append(HistoricalCommand(
                action=action, kind=kind, tool=tool, at=call.at, ref=call.ref,
                outcome=call.outcome, outcome_ref=call.outcome_ref, exit_code=call.exit_code,
            ))
        if len(hs.tool_events) < MAX_TOOL_EVENTS:
            hs.tool_events.append({"tool": tool, "at": call.at, "ref": call.ref, "command": command_index})
        for sha in call.commit_shas:
            if sha not in hs.tool_output_shas:
                hs.tool_output_shas.append(sha)
        if call.paths and call.outcome == OUTCOME_PASSED:
            base = call.cwd if isinstance(call.cwd, str) and call.cwd else session_cwd
            for raw, change_type in call.paths:
                rel = repo_relative(raw, repo_root, base)
                if rel is None:
                    hs.dropped[LOSS_PATH_OUTSIDE_REPO] = hs.dropped.get(LOSS_PATH_OUTSIDE_REPO, 0) + 1
                    continue
                prev = edits.get(rel)
                ct = "create" if prev is not None and prev.change_type == "create" and change_type != "delete" else change_type
                edits[rel] = HistoricalFileEdit(path=rel, change_type=ct, evidence=T, ref=call.ref)
    hs.file_edits = list(edits.values())[:MAX_FILES]
    hs.claimed_shas = list(parsed.claimed_shas)
    f["tool_calls"] = _fact(sum(hs.tool_counts.values()) or None, T, parsed.window_ref)
    return hs


def _same_dir(raw: str, repo_root: Path) -> bool:
    a = raw.rstrip("\\/").replace("\\", "/").lower()
    b = str(repo_root).rstrip("\\/").replace("\\", "/").lower()
    return a == b
