"""Text renderers for Receipts: the compact listing, the full receipt and the OSN sections.

Split out of ``history/shard_contract`` unchanged. The model (``ShardReceipt``
and friends), the builder (``build_shard_receipt``) and the display labels
stay there; this module only turns a built Receipt into text. The rendered
text is snapshot-tested, so nothing here changes what a Receipt says.
"""
from __future__ import annotations

import re
from datetime import datetime

from openshard.history.capture_completeness import (
    COMPLETENESS_INCOMPLETE,
    gaps_display,
)
from openshard.history.shard import (
    ORIGIN_EXTERNAL_OBSERVED,
    ORIGIN_HISTORICAL_IMPORT,
)
from openshard.history.shard_contract import (
    _CURSOR_USAGE_AGENTS,
    _EM,
    _FINDING_ICONS,
    _INDENT,
    _SEP,
    _UNICODE_OK,
    ShardFinding,
    ShardReceipt,
    _display_model_name,
    _file_line,
    _is_metadata_noise,
    _row,
    agent_reported_suffix,
    changed_files_display,
    checks_label,
    excluded_changes_rows,
    group_review_findings,
    permission_mode_label,
    verified_label,
)
from openshard.history.timeline import normalize_timeline
from openshard.history.usage_evidence import usage_line
from openshard.history.verification_truth import (
    INTEGRITY_NOTE,
    interpret_receipt,
    turn_status_label,
)


def _shows_usage_row(receipt: ShardReceipt) -> bool:
    """The Usage row: on Cursor-family Receipts, and on any Receipt later usage evidence strengthened."""
    usage = receipt.usage
    if not isinstance(usage, dict):
        return False
    return bool(usage.get("reconciled_by")) or usage.get("agent") in _CURSOR_USAGE_AGENTS

def _models_label_and_value(receipt: ShardReceipt) -> tuple[str, str]:
    """Return (label, value) for the Model/Models row in the compact receipt."""
    if receipt.model_stages:
        unique = list(dict.fromkeys(m for _, m in receipt.model_stages))
        if len(unique) == 1:
            return "Model", unique[0]
        if len(unique) == 2:
            return "Models", f"{unique[0]} → {unique[1]}"
        return "Models", ", ".join(unique)
    return "Model", receipt.model_display

def _truncate_compact(text: str, max_chars: int) -> str:
    """Return first line of text, word-safely truncated to max_chars with … if cut."""
    line = text.split("\n")[0]
    if len(line) <= max_chars:
        return line
    cut = line.rfind(" ", 0, max_chars)
    if cut > 0:
        return line[:cut] + "…"
    return line[:max_chars] + "…"

def _format_timeline_label(ev: dict) -> str:
    """Return an enriched display label for a timeline event using count/target fields."""
    key = ev.get("event", "")
    label = ev.get("label") or ""
    count = ev.get("count")
    target = ev.get("target")
    if key == "model_response_received" and target:
        return f"model responded: {target}"
    if key == "model_request_failed" and target:
        return f"model failed: {target}"
    if key == "review_checks_recorded" and count is not None:
        return f"checks recorded: {count}"
    if key == "static_findings_detected" and count is not None:
        return f"findings detected: {count}"
    if key == "receipt_saved" and target:
        return f"receipt saved: {target}"
    return label

def _format_token_count(n: int) -> str:
    """Compact display for a token count: 850 -> "850", 14000 -> "14k", 2500 -> "2.5k"."""
    if n < 1000:
        return str(n)
    k = n / 1000
    return f"{k:.0f}k" if k == int(k) else f"{k:.1f}k"

def _tool_activity_counts(receipt: ShardReceipt) -> list[tuple[str, int]]:
    """Per-tool invocation counts (e.g. [("Read", 3), ("Edit", 2)]), first-seen order.

    Derived at read time from receipt.events -- no new stored field. Every
    event this counts was already EVIDENCE_AGENT_REPORTED tool.invoked
    evidence; this only aggregates it for display, never claims an outcome.
    """
    from openshard.history.event import tool_identity

    counts: dict[str, int] = {}
    for ev in receipt.events:
        tool = tool_identity(ev)
        if tool is None:
            continue
        counts[tool] = counts.get(tool, 0) + 1
    return list(counts.items())

def _tool_failure_counts(receipt: ShardReceipt) -> dict[str, int]:
    """Per-tool count of ``tool.invoked`` events whose recorded status is ``failed``."""
    from openshard.history.event import tool_identity

    counts: dict[str, int] = {}
    for ev in receipt.events:
        tool = tool_identity(ev)
        if tool is None or getattr(ev, "status", None) != "failed":
            continue
        counts[tool] = counts.get(tool, 0) + 1
    return counts

_MAX_FAILED_ACTIVITY_ROWS = 5

_COMMAND_SAFETY_TAGS: dict[str, str] = {
    "blocked": "policy class: blocked",
    "needs_approval": "policy class: needs approval",
}

def failed_activity_rows(receipt: ShardReceipt) -> list[str]:
    """Human rows for failed tool calls: what ran, how it ended, and its policy class.

    The action text is the scrubbed, capped label the capture stored (never
    raw output); the exit code and the policy class come from the event's
    metadata. Activity failures are not verification: a failed ``rm`` is
    listed here, never counted as a check.
    """
    from openshard.history.event import tool_identity

    rows: list[str] = []
    failed = [
        ev for ev in receipt.events
        if tool_identity(ev) is not None and getattr(ev, "status", None) == "failed"
    ]
    for ev in failed[:_MAX_FAILED_ACTIVITY_ROWS]:
        meta = getattr(ev, "metadata", None) or {}
        parts: list[str] = []
        code = meta.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool):
            parts.append(f"exit {code}")
        source = meta.get("outcome_source") or getattr(ev, "evidence", None)
        if isinstance(source, str) and source:
            parts.append(source.replace("_", "-"))
        tag = _COMMAND_SAFETY_TAGS.get(str(meta.get("command_safety") or ""))
        if tag:
            parts.append(tag)
        action = getattr(ev, "action", None) or tool_identity(ev) or "tool call"
        rows.append(f"{_INDENT}  {action}" + (f"  ({'; '.join(parts)})" if parts else ""))
    if len(failed) > _MAX_FAILED_ACTIVITY_ROWS:
        rows.append(f"{_INDENT}  +{len(failed) - _MAX_FAILED_ACTIVITY_ROWS} more")
    return rows

_EVIDENCE_DISPLAY: dict[str, str] = {
    "directly_observed": "Directly observed",
    "agent_reported": "Agent reported",
    "git_observed": "Git observed",
    "independently_verified": "Independently verified",
    "git_verified": "Git verified",
    "imported_transcript": "Imported transcript",
}

_OBSERVER_DISPLAY: dict[str, str] = {
    "cursor_action_recording": "by Cursor Action Recording",
}

def _evidence_summary(receipt: ShardReceipt) -> list[str]:
    """Distinct evidence kinds behind this receipt's events, in a stable priority order.

    When every directly-observed event names the same known third-party
    observer, the label says who observed it.
    """
    seen = {getattr(ev, "evidence", None) for ev in receipt.events}
    order = [
        "independently_verified", "directly_observed", "git_verified", "git_observed",
        "imported_transcript", "agent_reported",
    ]
    out = [_EVIDENCE_DISPLAY[k] for k in order if k in seen]
    observers = {
        (getattr(ev, "metadata", None) or {}).get("observer")
        for ev in receipt.events
        if getattr(ev, "evidence", None) == "directly_observed"
    }
    if len(observers) == 1:
        observer = next(iter(observers))
        if isinstance(observer, str) and observer in _OBSERVER_DISPLAY:
            label = _EVIDENCE_DISPLAY["directly_observed"]
            out = [f"{label} ({_OBSERVER_DISPLAY[observer]})" if x == label else x for x in out]
    return out

def _capture_rows(receipt: ShardReceipt) -> list[str]:
    """The compact ``Capture`` / ``Gaps`` rows.

    ``Capture`` answers "how deep could OpenShard see?" (the unchanged
    capture depth; an externally observed run always says OpenShard did not
    execute or verify it). ``Gaps`` answers "is evidence known lost?":
    ``None known``, the loss itself (``1 queued event could not be decoded``),
    or ``Unknown`` for records written before loss tracking. The row is
    shown whenever the answer is not the default expectation, so a receipt
    with missing or unknowable evidence never looks like a healthy one.
    """
    rows: list[str] = []
    block = receipt.capture_completeness or {}
    status = block.get("status")
    if receipt.shard is not None and receipt.shard.origin == ORIGIN_EXTERNAL_OBSERVED:
        rows.append(_row(
            "Capture",
            f"{receipt.shard.capture_depth} {_EM} OpenShard did not execute or verify this run",
        ))
        rows.append(_row("Gaps", gaps_display(block)))
    elif receipt.shard is not None and receipt.shard.origin == ORIGIN_HISTORICAL_IMPORT:
        rows.append(_row("Capture", _HISTORICAL_CAPTURE_TEXT))
        rows.append(_row("Gaps", gaps_display(block)))
    elif status == COMPLETENESS_INCOMPLETE:
        rows.append(_row("Gaps", gaps_display(block)))
    return rows

_UNCONTROLLED_CONTROL_TEXT = (
    f"None {_EM} OpenShard only observed this agent; it could not block, approve or sandbox its actions"
)

_HISTORICAL_CAPTURE_TEXT = "Reconstructed from history; OpenShard did not observe this session live"

_CAPTURE_COL = 15  # the CAPTURE section's labels are longer than the receipt's default gutter

def _capture_rows_full(receipt: ShardReceipt) -> list[str]:
    """The full receipt's CAPTURE section: depth, completeness and known gaps as three facts."""
    block = receipt.capture_completeness or {}
    depth = str(block.get("depth") or (receipt.shard.capture_depth if receipt.shard else "unknown"))
    origin = receipt.shard.origin if receipt.shard is not None else None
    if origin == ORIGIN_EXTERNAL_OBSERVED:
        depth_text = f"{depth} {_EM} OpenShard did not execute or verify this run"
    elif origin == ORIGIN_HISTORICAL_IMPORT:
        depth_text = f"{depth} {_EM} {_HISTORICAL_CAPTURE_TEXT}"
    else:
        depth_text = depth
    return [
        f"{_INDENT}CAPTURE",
        _row("Capture depth", depth_text, width=_CAPTURE_COL),
        _row("Completeness", str(block.get("status") or "unknown").capitalize(), width=_CAPTURE_COL),
        _row("Known gaps", gaps_display(block), width=_CAPTURE_COL),
    ]

def _display_shard_number(shard_id: str) -> str:
    """Short human display; the full machine shard_id stays stored unchanged."""
    m = re.search(r"-(\d{4})$", shard_id or "")
    return f"#{m.group(1)}" if m else f"#{(shard_id or 'unknown')[-8:].upper()}"

def _display_receipt_number(receipt_id: str | None) -> str:
    """Canonical Receipt display id: no rcpt_ prefix, 12 hex chars like the design."""
    if not receipt_id:
        return "#UNKNOWN"
    value = receipt_id.removeprefix("rcpt_").replace("-", "")
    return f"#{value[:12].upper()}"

def _role_model_lines(rec: dict) -> list[str]:
    """One indented line per model when a role's calls went to more than one: calls, tokens, cost."""
    by_model = [m for m in (rec.get("by_model") or []) if isinstance(m, dict) and isinstance(m.get("model"), str)]
    if len(by_model) < 2:
        return []
    lines = []
    for m in by_model:
        parts = []
        attempts = [a for a in (m.get("attempts") or []) if isinstance(a, int)]
        if attempts:
            parts.append("attempt " + ", ".join(str(a) for a in attempts))
        if isinstance(m.get("calls"), int):
            parts.append(f"{m['calls']} call{'s' if m['calls'] != 1 else ''}")
        if isinstance(m.get("prompt_tokens"), int) or isinstance(m.get("completion_tokens"), int):
            parts.append(_format_token_count((m.get("prompt_tokens") or 0) + (m.get("completion_tokens") or 0)) + " tokens")
        cost = m.get("cost_usd")
        parts.append(f"${cost:.4f}" if isinstance(cost, (int, float)) and not isinstance(cost, bool) else "cost unknown")
        lines.append(f"{_INDENT}  ↳ {_display_model_name(m['model'])} · " + " · ".join(parts))
    return lines

def _role_line(role: str, rec: dict) -> str:
    """One role on one line: model, turns/calls, cost with provenance; or why it did not run."""
    status = rec.get("status")
    if status != "ran":
        why = rec.get("reason") or status or "not recorded"
        return _row(role.capitalize(), f"{status or 'not run'} ({why})", width=12)
    model = rec.get("model")
    by_model = [m for m in (rec.get("by_model") or []) if isinstance(m, dict) and isinstance(m.get("model"), str)]
    if len(by_model) > 1:
        # Several models made this role's calls (an escalation ladder): name them in
        # order rather than crediting the last one with every call.
        parts = [" → ".join(_display_model_name(m["model"]) for m in by_model)]
    else:
        parts = [_display_model_name(model) if isinstance(model, str) else "model unknown"]
    if isinstance(rec.get("turns"), int):
        parts.append(f"{rec['turns']} turn{'s' if rec['turns'] != 1 else ''}")
    elif isinstance(rec.get("calls"), int):
        parts.append(f"{rec['calls']} call{'s' if rec['calls'] != 1 else ''}")
    tokens = rec.get("total_tokens")
    if tokens is None and (rec.get("prompt_tokens") is not None or rec.get("completion_tokens") is not None):
        tokens = (rec.get("prompt_tokens") or 0) + (rec.get("completion_tokens") or 0)
    if isinstance(tokens, int):
        parts.append(_format_token_count(tokens) + " tokens")
    cost = rec.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        label = {"provider_reported": "provider-reported", "list_rate_estimate": "list-rate estimate"}.get(
            str(rec.get("cost_source") or ""), "origin not recorded",
        )
        parts.append(f"${cost:.4f} ({label})")
    else:
        parts.append("cost unknown")
    if rec.get("independent") is True:
        parts.append("independent")
    elif rec.get("independent") is False and role == "verifier":
        parts.append("same model as executor")
    return _row(role.capitalize(), " · ".join(parts), width=12)

def _explorer_line(ex: dict) -> str:
    """One parallel exploration worker: outcome, model, usage, cost with provenance."""
    model = ex.get("model")
    parts = [_display_model_name(model) if isinstance(model, str) else "model unknown", str(ex.get("status") or "?")]
    if isinstance(ex.get("findings_count"), int):
        parts.append(f"{ex['findings_count']} finding{'s' if ex['findings_count'] != 1 else ''}")
    cost = ex.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        label = {"provider_reported": "provider-reported", "list_rate_estimate": "list-rate estimate"}.get(
            str(ex.get("cost_source") or ""), "origin not recorded",
        )
        parts.append(f"${cost:.4f} ({label})")
    else:
        parts.append("cost unknown")
    idx = ex.get("index")
    label = f"  explorer {idx + 1}" if isinstance(idx, int) else "  explorer"
    return _row(label, " · ".join(parts), width=12)

def _render_osn_roles(receipt: ShardReceipt, *, detail: str = "compact") -> list[str]:
    """ROLES / PLAN / REVIEW sections for an OSN run with role evidence."""
    evidence = receipt.recorded_evidence or {}
    agent_loop_raw = evidence.get("agent_loop")
    agent_loop: dict = agent_loop_raw if isinstance(agent_loop_raw, dict) else {}
    roles = agent_loop.get("roles") if isinstance(agent_loop.get("roles"), dict) else None
    if not roles:
        return []
    lines = [f"{_INDENT}ROLES"]
    for role in ("planner", "executor", "verifier"):
        rec = roles.get(role)
        if isinstance(rec, dict):
            lines.append(_role_line(role, rec))
            for ex in (rec.get("explorers") or []) if role == "planner" else []:
                if isinstance(ex, dict):
                    lines.append(_explorer_line(ex))
            lines.extend(_role_model_lines(rec))
    lines.append("")
    if detail == "full":
        plan = agent_loop.get("plan") if isinstance(agent_loop.get("plan"), dict) else None
        if plan and plan.get("summary"):
            lines.append(f"{_INDENT}PLAN")
            lines.append(_row("Summary", str(plan["summary"]), width=12))
            if plan.get("file_count") is not None:
                lines.append(_row("Scope", f"{plan['file_count']} file(s), {plan.get('step_count') or 0} step(s)", width=12))
            lines.append("")
        reviews = agent_loop.get("reviews") if isinstance(agent_loop.get("reviews"), list) else []
        if reviews:
            lines.append(f"{_INDENT}REVIEW")
            for r in reviews:
                verdict = str(r.get("verdict") or "unknown").upper()
                model = r.get("model")
                head = f"{verdict} (model-reported" + (f", {_display_model_name(model)}" if isinstance(model, str) else "") + ")"
                lines.append(_row(f"Attempt {r.get('attempt')}", head, width=12))
                if r.get("summary"):
                    lines.append(_row("", str(r["summary"]), width=12))
                if r.get("recovery_requested"):
                    lines.append(_row("Recovery", str(r.get("recovery_outcome") or "requested"), width=12))
            lines.append(_row("Note", "A review never changes the verification result above.", width=12))
            lines.append("")
    return lines

def _render_osn_parallel(receipt: ShardReceipt) -> list[str]:
    """TOPOLOGY / WORKERS / SYNTHESIS / ECONOMICS sections for a run that decided its topology."""
    evidence = receipt.recorded_evidence or {}
    agent_loop_raw = evidence.get("agent_loop")
    agent_loop: dict = agent_loop_raw if isinstance(agent_loop_raw, dict) else {}
    topo = agent_loop.get("topology") if isinstance(agent_loop.get("topology"), dict) else None
    workers = agent_loop.get("workers") if isinstance(agent_loop.get("workers"), list) else []
    synth = agent_loop.get("synthesis") if isinstance(agent_loop.get("synthesis"), dict) else None
    cands = agent_loop.get("candidates") if isinstance(agent_loop.get("candidates"), dict) else None
    econ = agent_loop.get("economics") if isinstance(agent_loop.get("economics"), dict) else None
    resumed = agent_loop.get("resumed") if isinstance(agent_loop.get("resumed"), dict) else None
    graph = agent_loop.get("agents") if isinstance(agent_loop.get("agents"), dict) else None
    lines: list[str] = []
    if graph and graph.get("nodes"):
        lines.append(f"{_INDENT}AGENTS")
        for n in graph["nodes"]:
            model = n.get("model")
            cost = n.get("cost_usd")
            label = f"{n.get('role')}" + (f" (model {_display_model_name(model)})" if isinstance(model, str) else "")
            detail = (str(n.get("status") or "?") + (f" · {n['outcome']}" if n.get("outcome") else "")
                      + (f" · {n['calls']} call(s)" if n.get("calls") else "")
                      + (f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""))
            lines.append(_row(str(n.get("agent_id") or "agent"), f"{label} · {detail}", width=12))
        if graph.get("edges"):
            lines.append(_row("Edges", " · ".join(f"{e['from']} -{e['kind']}-> {e['to']}" for e in graph["edges"][:12]),
                              width=12))
        lines.append(_row("Evidence", str(graph.get("evidence") or "not recorded"), width=12))
        lines.append("")
    if resumed:
        lines.append(f"{_INDENT}RESUMED")
        how = (f" after {resumed['interrupted_reason']}" if resumed.get("interrupted_reason")
               else " after the process died unannounced" if resumed.get("checkpoint_status") == "running" else "")
        lines.append(_row("From", f"checkpoint '{resumed.get('checkpoint_phase')}'{how}", width=12))
        prior = resumed.get("prior_cost_usd")
        lines.append(_row("Carried", f"{resumed.get('attempts_restored') or 0} attempt(s) · "
                          f"{resumed.get('prior_model_calls') or 0} model call(s) · "
                          + (f"${prior:.4f}" if isinstance(prior, (int, float)) else "cost unknown")
                          + f" · {resumed.get('files_restored') or 0} file(s)", width=12))
        lines.append(_row("Note", "progress after the checkpoint was discarded; evidence recorded by OpenShard then",
                          width=12))
        lines.append("")
    if topo:
        lines.append(f"{_INDENT}TOPOLOGY")
        lines.append(_row("Selected", f"{topo.get('selected')} (requested {topo.get('requested')})", width=12))
        lines.append(_row("Reason", str(topo.get("reason") or "not recorded"), width=12))
        if topo.get("worker_count"):
            cost = topo.get("actual_extra_cost_usd")
            extra = f" · extra cost ${cost:.4f}" if isinstance(cost, (int, float)) else ""
            lines.append(_row("Workers", f"{topo['worker_count']} · {topo.get('distinct_models') or '?'} distinct model(s){extra}", width=12))
        lines.append("")
    if workers:
        lines.append(f"{_INDENT}WORKERS")
        for w in workers:
            model = w.get("model")
            cost = w.get("cost_usd")
            label = {"provider_reported": "provider-reported", "list_rate_estimate": "list-rate estimate"}.get(
                str(w.get("cost_source") or ""), "origin not recorded")
            cost_text = f"${cost:.4f} ({label})" if isinstance(cost, (int, float)) else "cost unknown"
            status = str(w.get("status") or "?") + (f" ({w.get('reason')})" if w.get("reason") else "")
            own = f" · own-copy check {w['own_copy_verification']}" if w.get("own_copy_verification") else ""
            lines.append(_row(str(w.get("worker_id") or "worker"),
                              f"{w.get('subtask_id')} · {status} · "
                              f"{_display_model_name(model) if isinstance(model, str) else 'model unknown'} · "
                              f"{w.get('turns') or 0} turn(s) · {w.get('files_changed') or 0} file(s) · {cost_text}{own}",
                              width=12))
        lines.append("")
    if cands:
        lines.append(f"{_INDENT}CANDIDATES")
        lines.append(_row("Policy", str(cands.get("policy") or "not recorded"), width=12))
        winner = cands.get("winner")
        wm = cands.get("winner_model")
        lines.append(_row("Winner", f"{winner} · {_display_model_name(wm) if isinstance(wm, str) else 'model unknown'}"
                          if winner else f"none ({cands.get('reason') or 'no candidate verified'})", width=12))
        for e in cands.get("evaluated") or []:
            cost = e.get("cost_usd")
            model = e.get("model")
            lines.append(_row(f"#{e.get('rank')} {e.get('worker_id')}",
                              f"{_display_model_name(model) if isinstance(model, str) else 'model unknown'} · "
                              f"own-copy verification {e.get('verification') or 'not run'} · "
                              f"{e.get('files_changed') or 0} file(s) · "
                              + (f"${cost:.4f}" if isinstance(cost, (int, float)) else "cost unknown")
                              + (" · selected" if e.get("selected") else ""), width=12))
        lines.append(_row("Evidence", str(cands.get("verification_evidence") or "not recorded"), width=12))
        lines.append("")
    if synth:
        lines.append(f"{_INDENT}SYNTHESIS")
        lines.append(_row("Applied", f"{synth.get('applied_count') or 0} file(s) from {synth.get('workers_accepted') or 0} worker(s)", width=12))
        lines.append(_row("Conflicts", f"{synth.get('conflict_count') or 0} · rejected {synth.get('rejected_count') or 0} · missing required {synth.get('missing_required') or 0}", width=12))
        lines.append(_row("Resolution", str(synth.get("resolution") or "not recorded"), width=12))
        lines.append("")
    if econ and econ.get("total_cost_usd") is not None:
        lines.append(f"{_INDENT}ECONOMICS")
        total = econ["total_cost_usd"]
        lines.append(_row("Run cost", f"${total:.4f}" + ("" if econ.get("cost_complete") else " (incomplete)"), width=12))
        cpvs = econ.get("cost_per_verified_success")
        lines.append(_row("Per success", f"${cpvs:.4f}" if isinstance(cpvs, (int, float)) else "not verified", width=12))
        by_role = econ.get("by_role") or {}
        if by_role:
            lines.append(_row("By role", " · ".join(
                f"{r} ${c:.4f}" if isinstance(c, (int, float)) else f"{r} unknown" for r, c in by_role.items()), width=12))
        lines.append("")
    return lines

def _render_osn_actions(receipt: ShardReceipt) -> list[str]:
    """The OSN ACTIONS section of the full Receipt: what the agent asked, what OpenShard decided and saw."""
    if not receipt.osn_actions:
        return []
    lines = [f"{_INDENT}OSN ACTIONS"]
    attempts = {a["attempt"] for a in receipt.osn_actions if a.get("attempt") is not None}
    cap = 40
    for a in receipt.osn_actions[:cap]:
        prefix = f"a{a['attempt']} " if len(attempts) > 1 and a.get("attempt") is not None else ""
        turn = f"t{a['turn']} " if a.get("turn") is not None else ""
        if a.get("decision") in (None, "not_applicable"):
            outcome = "ok" if a.get("ok") else "failed" if a.get("executed") else "-"
        elif a.get("executed"):
            outcome = f"{a['decision']} · {'ok' if a.get('ok') else 'failed'}"
        else:
            outcome = f"{a['decision']} · refused"
        target = a.get("target") or ""
        summary = a.get("summary") or ""
        detail = " · ".join(x for x in (target, summary) if x and x != target or x == target and not summary)
        if target and summary and summary.startswith(("update:", "create:", "unchanged:")):
            detail = summary
        lines.append(f"{_INDENT}  {prefix}{turn}{str(a['kind']).ljust(16)} {outcome.ljust(16)} {detail}".rstrip())
        if a.get("intent"):
            lines.append(f"{_INDENT}      ↳ {a['intent']}")
    if len(receipt.osn_actions) > cap:
        lines.append(f"{_INDENT}  +{len(receipt.osn_actions) - cap} more")
    lines.append("")
    return lines

def _osn_checks_display(receipt: ShardReceipt, loop: dict) -> str:
    if receipt.checks_display != "Not run":
        return checks_label(receipt)
    reason = loop.get("stop_reason")
    if reason == "provider_error":
        return "Not run — provider failed before verification"
    if reason == "verifier_setup_failed":
        return "Not run — verifier could not start"
    if reason == "budget_exhausted":
        return "Not run — budget stopped the run"
    return checks_label(receipt)

def _first_attempt_cost_label(selected: str, evidence: dict) -> str:
    """The COST section's first row: the first attempt's model, or 'all roles' when other roles' cost is in it.

    The figure is the run's recorded cost minus the retries', so when a planner
    or verifier made calls it is theirs too; naming only the executor's model
    credited it with their spend. ROLES carries the per-role split.
    """
    agent_loop_raw = evidence.get("agent_loop")
    agent_loop: dict = agent_loop_raw if isinstance(agent_loop_raw, dict) else {}
    roles_raw = agent_loop.get("roles")
    roles: dict = roles_raw if isinstance(roles_raw, dict) else {}
    active = []
    for r in ("planner", "executor", "verifier"):
        rec = roles.get(r)
        if isinstance(rec, dict) and rec.get("status") == "ran" and (rec.get("calls") or 0) > 0:
            active.append(r)
    if len(active) > 1:
        return "Attempt 1 · all roles"  # which roles: the ROLES section above
    return selected

def _osn_repository_state_line(raw: object, files_changed: int) -> str | None:
    """Where the changed files were when the Receipt was written: the isolated copy, or the repository.

    Only for runs that recorded it; older Receipts say nothing rather than guess.
    """
    if not isinstance(raw, dict) or "applied" not in raw or not files_changed:
        return None
    if raw.get("applied"):
        skipped = raw.get("files_skipped") or 0
        text = "applied to the repository"
        if isinstance(skipped, int) and skipped > 0:
            text += f" · {skipped} skipped by policy"
        return text
    return "in an isolated copy · not applied when this Receipt was written"

def _render_osn_compact_receipt(receipt: ShardReceipt) -> str:
    """Routing-first OSN Receipt based on the canonical Receipt design."""
    evidence = receipt.recorded_evidence or {}
    routing = evidence.get("adaptive_routing") or {}
    retry = evidence.get("retry") or {}
    loop = evidence.get("execution_loop") or {}
    supervisor = evidence.get("supervisor_routing") or {}
    is_routing = isinstance(routing, dict) and routing.get("applied") is True
    kind = "ROUTING RECEIPT" if is_routing else "PROOF RECEIPT"

    lines = [
        _SEP,
        f"{_INDENT}OPENSHARD · {kind}",
        f"{_INDENT}SHARD {_display_shard_number(receipt.shard_id)}    RECEIPT {_display_receipt_number(receipt.receipt_id)}",
        _SEP,
        f"{_INDENT}{receipt.task_short}",
        "",
        _row("Agent", "Openshard Native (OSN)"),
    ]
    if receipt.repo:
        lines.append(_row("Repo", receipt.repo_identity or receipt.repo))
    if receipt.branch:
        lines.append(_row("Branch", receipt.branch))
    if receipt.owner:
        lines.append(_row("Owner", receipt.owner))

    selected_raw = routing.get("selected_model") if isinstance(routing, dict) else None
    selected = _display_model_name(selected_raw) if isinstance(selected_raw, str) else receipt.model_display
    retry_attempts = retry.get("attempts") if isinstance(retry, dict) else None
    retry_attempts = retry_attempts if isinstance(retry_attempts, list) else []
    ladder = routing.get("escalation_ladder") if isinstance(routing, dict) else None
    ladder = ladder if isinstance(ladder, list) else []

    lines += ["", f"{_INDENT}ROUTE", f"{_INDENT}{selected}"]
    if retry_attempts:
        for attempt in retry_attempts:
            model = attempt.get("model") if isinstance(attempt, dict) else None
            if not isinstance(model, str):
                continue
            lines.append(f"{_INDENT}  ↓ verification failed")
            lines.append(f"{_INDENT}{_display_model_name(model)}")
        if receipt.verification_status == "passed":
            lines.append(f"{_INDENT}  ↓ verification passed")
    elif loop.get("stop_reason") == "provider_error":
        lines.append(f"{_INDENT}  ↓ provider error")
        if ladder:
            lines.append(f"{_INDENT}Fallback planned · {_display_model_name(str(ladder[0]))} · not used")
    elif receipt.verification_status == "passed":
        lines.append(f"{_INDENT}  ↓ verification passed")
    elif receipt.verification_status == "failed":
        lines.append(f"{_INDENT}  ↓ verification failed")

    _role_lines = _render_osn_roles(receipt)
    if _role_lines:
        lines += ["", *_role_lines[:-1]]

    lines += [
        "",
        f"{_INDENT}WORK",
        _row("Files modified", str(receipt.files_changed), width=16),
    ]
    if receipt.files_touched:
        lines.append(f"{_INDENT}  ↳ " + " · ".join(receipt.files_touched[:5]))
    _agent_loop_raw = evidence.get("agent_loop")
    _agent_loop: dict = _agent_loop_raw if isinstance(_agent_loop_raw, dict) else {}
    _repo_state = _osn_repository_state_line(_agent_loop.get("repository"), receipt.files_changed)
    if _repo_state:
        lines.append(f"{_INDENT}  ↳ {_repo_state}")
    _acts = _agent_loop.get("action_summary")
    if isinstance(_acts, dict) and _acts:
        _turns = _agent_loop.get("turns_total")
        _parts = [f"{_acts.get('actions', 0)} actions"]
        if _acts.get("reads") or _acts.get("searches") or _acts.get("listings"):
            _parts.append(
                f"{(_acts.get('reads') or 0) + (_acts.get('searches') or 0) + (_acts.get('listings') or 0)} inspect"
            )
        _parts.append(f"{_acts.get('writes_applied', 0)} writes")
        if _acts.get("writes_blocked"):
            _parts.append(f"{_acts['writes_blocked']} refused")
        _parts.append(f"{_acts.get('verifications', 0)} verify")
        _head = f"{_turns} turns · " if isinstance(_turns, int) else ""
        lines.append(_row("Agent loop", _head + " · ".join(_parts), width=16))
    if receipt.duration_seconds is not None:
        minutes, seconds = divmod(int(round(receipt.duration_seconds)), 60)
        duration = f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"
        lines.append(_row("Duration", duration))
    lines.append(_row("Checks", _osn_checks_display(receipt, loop)))

    lines += ["", f"{_INDENT}COST"]
    total = receipt.cost_raw
    retry_costs: list[float] = []
    for attempt in retry_attempts:
        if not isinstance(attempt, dict):
            continue
        retry_cost = attempt.get("cost_usd")
        if isinstance(retry_cost, (int, float)) and not isinstance(retry_cost, bool):
            retry_costs.append(float(retry_cost))
    first_cost = total
    if total is not None and retry_attempts and len(retry_costs) == len(retry_attempts):
        first_cost = max(0.0, total - sum(retry_costs))
    if first_cost is not None:
        lines.append(_row(_first_attempt_cost_label(selected, evidence), "$" + f"{first_cost:.4f}", width=28))
    for attempt in retry_attempts:
        if not isinstance(attempt, dict) or not isinstance(attempt.get("model"), str):
            continue
        cost = attempt.get("cost_usd")
        cost_text = "$" + f"{cost:.4f}" if isinstance(cost, (int, float)) else "not recorded"
        lines.append(_row(_display_model_name(attempt["model"]), cost_text, width=28))
    if not retry_attempts and ladder and loop.get("stop_reason") == "provider_error":
        lines.append(_row(_display_model_name(str(ladder[0])), "not used", width=28))
    lines.append(_row("Run cost · recorded", receipt.cost_display, width=28))

    lines += [
        "",
        f"{_INDENT}PROOF",
        _row("Verification", verified_label(receipt), width=14),
        _row("Integrity", receipt.integrity),
    ]
    if is_routing:
        policy = routing.get("policy") or {}
        version = policy.get("version") if isinstance(policy, dict) else None
        lines.append(_row("Routing", f"Adaptive V2{f' · v{version}' if version else ''}"))
    if isinstance(supervisor, dict):
        decisions = supervisor.get("decisions") or []
        lines.append(_row("Supervisor", "Used" if decisions else "Not consulted"))

    if receipt.tokens_input is not None or receipt.tokens_output is not None:
        total_tokens = (receipt.tokens_input or 0) + (receipt.tokens_output or 0)
        lines.append(_row("Tokens", _format_token_count(total_tokens)))

    if receipt.error_class:
        result_label = "PROVIDER ERROR" if receipt.error_class == "provider_error" else receipt.error_class.upper()
    elif receipt.verification_status == "passed":
        result_label = "VERIFIED"
    elif receipt.verification_status == "failed":
        result_label = "FAILED VERIFICATION"
    else:
        result_label = receipt.status.upper()

    lines += ["", f"{_INDENT}RESULT", f"{_INDENT}{result_label}", f"{_INDENT}{receipt.human_summary or receipt.result}", _SEP]
    return "\n".join(lines)


def lines_changed_display(receipt: ShardReceipt) -> str:
    """``+N / −M`` when both counts were recorded, else ``not captured yet`` (unknown is never 0)."""
    if receipt.diff_added is None or receipt.diff_removed is None:
        return "not captured yet"
    minus = "\u2212" if _UNICODE_OK else "-"
    return f"+{receipt.diff_added} / {minus}{receipt.diff_removed}"


def render_compact_shard_receipt(receipt: ShardReceipt) -> str:
    """Render a bordered, column-aligned RECEIPT block. Pure, no I/O."""
    evidence = receipt.recorded_evidence or {}
    if isinstance(evidence.get("execution_loop"), dict):
        return _render_osn_compact_receipt(receipt)

    model_label, model_value = _models_label_and_value(receipt)

    lines = [
        _SEP,
        f"{_INDENT}RECEIPT {_EM} {receipt.shard_id}",
        _SEP,
        _row("Task", receipt.task_short),
        _row("Executor", receipt.agent),
    ]
    if receipt.receipt_id:
        lines.append(_row("Receipt ID", receipt.receipt_id))
    if receipt.task_id:
        lines.append(_row("Task ID", receipt.task_id))
    lines += _capture_rows(receipt)
    if receipt.task_completion:
        # Resolved by later evidence (see verification_truth.turn_status_label)
        # so this row never contradicts the Verified row below it.
        lines.append(_row("Status", turn_status_label(receipt.task_completion, interpret_receipt(receipt))
                          or receipt.task_completion))
    lines.append(_row(model_label, model_value))
    if receipt.duration_seconds is not None:
        lines.append(_row("Duration", f"{receipt.duration_seconds:.1f}s"))
    lines.append(_row("Changed", changed_files_display(receipt)))
    lines.append(_row("Lines changed", lines_changed_display(receipt), width=15))
    lines += excluded_changes_rows(receipt)
    if receipt.files_detail:
        lines.append(f"{_INDENT}Files")
        for _fd in receipt.files_detail[:10]:
            if not isinstance(_fd, dict):
                continue
            _line = _file_line(_fd)
            if _line:
                lines.append(_line)
        if len(receipt.files_detail) > 10:
            lines.append(f"{_INDENT}  +{len(receipt.files_detail) - 10} more")
    _activity = _tool_activity_counts(receipt)
    if _activity:
        _failed_by_tool = _tool_failure_counts(receipt)
        lines.append(f"{_INDENT}Activity")
        for tool, count in _activity:
            _nf = _failed_by_tool.get(tool, 0)
            lines.append(f"{_INDENT}  {tool} × {count}" + (f" ({_nf} failed)" if _nf else ""))
        _failed_rows = failed_activity_rows(receipt)
        if _failed_rows:
            lines.append(f"{_INDENT}Failed")
            lines.extend(_failed_rows)
    lines += [
        _row("Checks", checks_label(receipt)),
        _row("Verified", verified_label(receipt)),
        _row("Integrity", receipt.integrity),
        _row("Risk", receipt.risk),
        _row("Sandbox", receipt.sandbox),
        _row("Approval", receipt.approval),
    ]
    _permissions = permission_mode_label(receipt)
    if _permissions:
        lines.append(_row("Permissions", _permissions))
    lines.append(_row("Cost", receipt.cost_display))
    if receipt.tokens_input is not None or receipt.tokens_output is not None:
        _tok_in = _format_token_count(receipt.tokens_input or 0)
        _tok_out = _format_token_count(receipt.tokens_output or 0)
        _tok_line = f"{_tok_in} input / {_tok_out} output"
        if receipt.tokens_cache_read:
            _tok_line += f" (+{_format_token_count(receipt.tokens_cache_read)} cache read)"
        lines.append(_row("Tokens", _tok_line))
    if _shows_usage_row(receipt):
        lines.append(_row("Usage", usage_line(receipt.usage)))
    lines.append(_row("Result", receipt.result))
    _secret_count = sum(1 for c in receipt.evidence_capsules if c.kind == "secret_scan")
    if _secret_count:
        lines.append(_row("Secrets", f"{_secret_count} finding(s) — see full receipt"))
    if receipt.developer_feedback:
        lines.append(_row("Feedback", receipt.developer_feedback.get("outcome", "")))
    _evidence = _evidence_summary(receipt)
    if _evidence:
        lines.append(_row("Evidence", ", ".join(_evidence)))

    _TOP_SEVERITIES = {"Critical", "High", "Medium"}
    top_raw = [f for f in receipt.findings if f.severity in _TOP_SEVERITIES]
    if top_raw:
        # Use generous per-severity caps — the hard limit is the total of 5 visible slots.
        # group_review_findings handles dedup and metadata grouping.
        visible_sub, meta_group, _, _ = group_review_findings(
            top_raw,
            caps={"Critical": 5, "High": 5, "Medium": 5, "Low": 0},
        )
        compact_list: list[ShardFinding] = list(visible_sub)
        if meta_group and len(compact_list) < 5:
            compact_list.append(meta_group)
        compact_list = compact_list[:5]
        hidden_receipt = max(0, len(top_raw) - len(compact_list))
        lines.append(_SEP)
        lines.append(f"{_INDENT}FINDINGS")
        for f in compact_list:
            icon = _FINDING_ICONS.get(f.severity, "⚠" if _UNICODE_OK else "!")
            lines.append(f"{_INDENT}{icon}  {_truncate_compact(f.message, 79)}")
        if hidden_receipt > 0:
            lines.append(f"{_INDENT}+{hidden_receipt} more findings recorded.")

    # Warning line: only shown when a note contains an explicit blocker keyword
    _WARNING_KEYWORDS = ("DO NOT RUN", "WARNING:", "BLOCKER:", "DANGER:", "DO NOT APPLY")
    warning_note = next(
        (n for n in receipt.agent_notes if any(kw in n.upper() for kw in _WARNING_KEYWORDS)),
        None,
    )
    if warning_note:
        lines += [_SEP, f"{_INDENT}{warning_note}"]

    lines.append(_SEP)
    return "\n".join(lines)

def _fmt_timestamp(ts: str) -> str:
    if not ts:
        return "-"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, AttributeError):
        return ts

def render_full_shard_receipt(receipt: ShardReceipt, detail: str = "full") -> str:
    """Render a full structured SHARD block with consistent separator style. Pure, no I/O."""
    lines: list[str] = []

    lines += [_SEP, f"{_INDENT}SHARD {_EM} {receipt.shard_id}", _SEP, ""]

    if receipt.schema_version:
        lines.append(f"{_INDENT}SCHEMA")
        lines.append(_row("Version", receipt.schema_version))
        lines.append("")

    lines += [f"{_INDENT}TASK", f"{_INDENT}{receipt.task_full}", ""]

    lines.append(f"{_INDENT}EXECUTION")
    lines.append(_row("Executor", receipt.agent))
    lines.append(_row("Strategy", receipt.strategy))

    if receipt.model_stages:
        unique = list(dict.fromkeys(m for _, m in receipt.model_stages))
        if len(unique) == 1:
            lines.append(_row("Model", unique[0]))
        else:
            lines.append(f"{_INDENT}Models")
            _stage_col = max(len(s) for s, _ in receipt.model_stages) + 2
            for stage_lbl, model_name in receipt.model_stages:
                lines.append(f"{_INDENT}  {stage_lbl:<{_stage_col}}{model_name}")
    else:
        lines.append(_row("Model", receipt.model_display))

    dur = f"{receipt.duration_seconds:.1f}s" if receipt.duration_seconds is not None else "-"
    lines.append(_row("Duration", dur))
    lines.append(_row("Status", receipt.status + agent_reported_suffix(receipt)))
    if receipt.attempt_number is not None:
        lines.append(_row("Attempt", f"{receipt.attempt_number} (Shard {receipt.shard_id})"))
    lines.append("")
    lines += _capture_rows_full(receipt)
    lines.append("")

    if receipt.adapter:
        lines.append(f"{_INDENT}ADAPTER")
        lines.append(_row("Name", receipt.adapter))
        if receipt.adapter_available is not None:
            lines.append(_row("Available", "yes" if receipt.adapter_available else "no"))
        if receipt.adapter_exit_code is not None:
            lines.append(_row("Exit code", str(receipt.adapter_exit_code)))
        if receipt.adapter_duration_ms is not None:
            lines.append(_row("Duration", f"{receipt.adapter_duration_ms} ms"))
        if receipt.adapter_command:
            _cmd_tokens = receipt.adapter_command[:3]
            _cmd_preview = " ".join(_cmd_tokens)
            if len(receipt.adapter_command) > 3:
                _cmd_preview += " …"
            lines.append(_row("Command", _cmd_preview[:120]))
        if receipt.adapter_stdout_summary:
            lines.append(_row("Stdout", receipt.adapter_stdout_summary[:200]))
        if receipt.adapter_stderr_summary:
            lines.append(_row("Stderr", receipt.adapter_stderr_summary[:200]))
        lines.append("")

    if receipt.error_class:
        lines.append(f"{_INDENT}ERROR")
        lines.append(_row("Class", receipt.error_class))
        if receipt.error_message:
            lines.append(_row("Message", receipt.error_message[:120]))
        lines.append("")

    if receipt.run_timeline:
        _chk = "✓" if _UNICODE_OK else "+"
        _fail = "✖" if _UNICODE_OK else "x"
        lines.append(f"{_INDENT}TIMELINE")
        _tl_events = normalize_timeline(receipt.run_timeline)
        _receipt_ev = next((e for e in _tl_events if e.get("event") == "receipt_saved"), None)
        _regular_evs = [e for e in _tl_events if e.get("event") != "receipt_saved"]
        _has_checks_ev = any(e.get("event") == "review_checks_recorded" for e in _regular_evs)
        _has_risk_ev = any(e.get("event") == "risk_classified" for e in _regular_evs)
        _has_inspected_ev = any(e.get("event") == "files_inspected" for e in _regular_evs)
        for _ev in _regular_evs:
            _sym = _chk if _ev.get("status", "completed") != "failed" else _fail
            lines.append(f"{_INDENT}  {_sym} {_format_timeline_label(_ev)}")
        # Synthesise proof facts from receipt fields when not covered by stored events
        if not _has_inspected_ev and receipt.files_read_count:
            lines.append(f"{_INDENT}  {_chk} files inspected: {receipt.files_read_count}")
        if not _has_risk_ev and receipt.risk and receipt.risk not in ("Not recorded", "-", ""):
            lines.append(f"{_INDENT}  {_chk} risk classified: {receipt.risk}")
        if not _has_checks_ev and receipt.checks_display and receipt.checks_display != "Not run":
            lines.append(f"{_INDENT}  {_chk} checks: {checks_label(receipt)}")
        # receipt_saved always last
        if _receipt_ev:
            _sym = _chk if _receipt_ev.get("status", "completed") != "failed" else _fail
            lines.append(f"{_INDENT}  {_sym} {_format_timeline_label(_receipt_ev)}")
        lines.append("")

    lines.append(f"{_INDENT}CONTEXT")
    lines.append(_row("Repo", receipt.repo or "Not recorded"))
    lines.append(_row("Branch", receipt.branch or "Not recorded"))
    lines.append(_row("Git state", receipt.git_state or "Not recorded"))
    lines.append(_row("Quality", receipt.context_quality or "Not recorded"))
    if receipt.files_read_count is not None:
        lines.append(_row("Read", f"{receipt.files_read_count} file{'s' if receipt.files_read_count != 1 else ''}"))
    else:
        lines.append(_row("Read", "Not recorded"))
    touched_count = len(receipt.files_touched)
    if touched_count > 0:
        lines.append(_row("Touched", f"{touched_count} file{'s' if touched_count != 1 else ''}"))
    else:
        lines.append(_row("Touched", "-"))
    lines.append("")

    _git_new = (
        receipt.git_head_commit_hash is not None
        or receipt.git_base_branch is not None
        or receipt.git_base_commit_hash is not None
        or receipt.git_dirty is not None
        or receipt.safe_workspace_display_name is not None
    )
    if _git_new:
        lines.append(f"{_INDENT}GIT")
        if receipt.git_head_commit_hash:
            lines.append(_row("Head commit", receipt.git_head_commit_hash))
        if receipt.git_base_branch:
            lines.append(_row("Base branch", receipt.git_base_branch))
        if receipt.git_base_commit_hash:
            lines.append(_row("Base commit", receipt.git_base_commit_hash))
        if receipt.git_dirty is not None:
            lines.append(_row("Dirty", "yes" if receipt.git_dirty else "no"))
        if receipt.safe_workspace_kind and receipt.safe_workspace_kind != "none":
            lines.append(_row("Workspace", receipt.safe_workspace_kind))
        if receipt.safe_workspace_display_name:
            lines.append(_row("Workspace ID", receipt.safe_workspace_display_name))
        lines.append("")

    _ctx_util = (
        receipt.context_files_considered_count is not None
        or receipt.context_files_injected_count is not None
        or receipt.context_utilisation_ratio is not None
    )
    if _ctx_util:
        lines.append(f"{_INDENT}CONTEXT USAGE")
        if receipt.context_files_considered_count is not None:
            lines.append(_row("Considered", str(receipt.context_files_considered_count)))
        if receipt.context_files_injected_count is not None:
            lines.append(_row("Injected", str(receipt.context_files_injected_count)))
        if receipt.context_utilisation_ratio is not None:
            lines.append(_row("Utilisation", f"{receipt.context_utilisation_ratio:.0%}"))
        lines.append("")

    lines.append(f"{_INDENT}FILE EVIDENCE")
    lines.append("")
    _fe_cap = 10
    _fe_inspected = [fe for fe in receipt.file_evidence if "inspected" in fe.roles]
    _fe_findings = [fe for fe in receipt.file_evidence if "finding_source" in fe.roles]
    _fe_changed = [fe for fe in receipt.file_evidence if "changed" in fe.roles]
    for _fe_heading, _fe_group in (
        ("INSPECTED FILES", _fe_inspected),
        ("FILES WITH FINDINGS", _fe_findings),
    ):
        if _fe_group:
            lines.append(f"{_INDENT}  {_fe_heading}")
            for _fe in _fe_group[:_fe_cap]:
                lines.append(f"{_INDENT}    {_fe.path}")
            if len(_fe_group) > _fe_cap:
                lines.append(f"{_INDENT}    +{len(_fe_group) - _fe_cap} more")
            lines.append("")
    lines.append(f"{_INDENT}  CHANGED FILES")
    if _fe_changed:
        for _fe in _fe_changed[:_fe_cap]:
            lines.append(f"{_INDENT}    {_fe.path}")
        if len(_fe_changed) > _fe_cap:
            lines.append(f"{_INDENT}    +{len(_fe_changed) - _fe_cap} more")
    else:
        lines.append(f"{_INDENT}    none")
    lines.append("")

    if receipt.evidence_capsules:
        lines.append(f"{_INDENT}EVIDENCE CAPSULES")
        _ec_cap = 10
        for _ec in receipt.evidence_capsules[:_ec_cap]:
            _ec_loc = f"  [{_ec.path}:{_ec.line}]" if _ec.path and _ec.line is not None else (f"  [{_ec.path}]" if _ec.path else "")
            lines.append(f"{_INDENT}  {_ec.kind}  {_ec.summary}{_ec_loc}")
        if len(receipt.evidence_capsules) > _ec_cap:
            lines.append(f"{_INDENT}  +{len(receipt.evidence_capsules) - _ec_cap} more")
        lines.append("")

    lines.append(f"{_INDENT}CHANGES")
    file_str = changed_files_display(receipt)
    if receipt.diff_added is not None and receipt.diff_removed is not None:
        file_str += f" changed (+{receipt.diff_added} / -{receipt.diff_removed})"
    elif receipt.files_changed > 0 and not receipt.changes:
        file_str += " changed"
    lines.append(f"{_INDENT}{file_str}")
    for _fd in receipt.files_detail[:10]:
        if isinstance(_fd, dict) and "path" in _fd:
            _line = _file_line(_fd)
            if _line:
                lines.append(_line)
    if len(receipt.files_detail) > 10:
        lines.append(f"{_INDENT}  (+{len(receipt.files_detail) - 10} more)")
    lines += excluded_changes_rows(receipt)
    if receipt.files_excluded:
        lines.append(f"{_INDENT}  Not counted (git showed these; not this run's work):")
        for _fd in receipt.files_excluded[:10]:
            _line = _file_line(_fd)
            if _line:
                lines.append("  " + _line)
        if len(receipt.files_excluded) > 10:
            lines.append(f"{_INDENT}    (+{len(receipt.files_excluded) - 10} more)")
    if receipt.changes and receipt.changes.get("baseline", {}).get("source") == "git_status":
        _bl = receipt.changes["baseline"]
        _bl_text = f"{_bl.get('dirty_paths', 0)} path(s) already changed at session start"
        if _bl.get("truncated"):
            _bl_text += " (list truncated)"
        lines.append(_row("Baseline", _bl_text))
    lines.append("")

    lines.append(f"{_INDENT}COST")
    lines.append(f"{_INDENT}{receipt.cost_display}")
    if receipt.tokens_input is not None or receipt.tokens_output is not None:
        _tok_in = _format_token_count(receipt.tokens_input or 0)
        _tok_out = _format_token_count(receipt.tokens_output or 0)
        lines.append(_row("Tokens", f"{_tok_in} input / {_tok_out} output"))
    if _shows_usage_row(receipt):
        lines.append(_row("Usage", usage_line(receipt.usage)))
    lines.append("")

    lines.append(f"{_INDENT}CHECKS")
    if receipt.check_results:
        for cr in receipt.check_results:
            _cr = cr if detail == "full" else _truncate_compact(cr, 90)
            lines.append(f"{_INDENT}  {_cr}")
    else:
        lines.append(f"{_INDENT}{checks_label(receipt)}")
    lines.append(_row("Verified", verified_label(receipt)))
    lines.append("")

    lines.append(f"{_INDENT}POLICY")
    lines.append(_row("Risk", receipt.risk))
    lines.append(_row("Sandbox", receipt.sandbox))
    if receipt.allowed_paths:
        lines.append(_row("Allowed", ", ".join(receipt.allowed_paths[:3])))
    if receipt.blocked_paths:
        lines.append(_row("Blocked", ", ".join(receipt.blocked_paths[:3])))
    if receipt.blocked_commands:
        lines.append(_row("Commands", f"{len(receipt.blocked_commands)} blocked"))
    lines.append(_row("Approval", receipt.approval))
    if receipt.shard is not None and receipt.shard.origin in (ORIGIN_EXTERNAL_OBSERVED, ORIGIN_HISTORICAL_IMPORT):
        lines.append(_row("Control", _UNCONTROLLED_CONTROL_TEXT))
    lines.append("")

    if receipt.approval_required:
        lines.append(f"{_INDENT}APPROVAL")
        lines.append(_row("Required", "yes"))
        lines.append(_row("Status", "granted" if receipt.approval_granted else "denied"))
        if receipt.approval_reason:
            lines.append(_row("Reason", receipt.approval_reason))
        if not receipt.approval_granted:
            lines.append(_row("Result", "Writes blocked"))
        lines.append("")

    if receipt.policy_decisions:
        lines.append(f"{_INDENT}POLICY DECISIONS")
        _pd_cap = 10
        _pd_col_dec = 6
        _pd_col_act = 16
        for _pd in receipt.policy_decisions[:_pd_cap]:
            _pd_dec = str(_pd.get("decision") or "").ljust(_pd_col_dec)
            _pd_act = str(_pd.get("action") or "").ljust(_pd_col_act)
            _pd_reason = str(_pd.get("reason") or "")
            lines.append(f"{_INDENT}  {_pd_dec}  {_pd_act}  {_pd_reason}")
        if len(receipt.policy_decisions) > _pd_cap:
            lines.append(f"{_INDENT}  +{len(receipt.policy_decisions) - _pd_cap} more")
        lines.append("")

    lines.extend(_render_osn_roles(receipt, detail="full"))
    lines.extend(_render_osn_parallel(receipt))
    lines.extend(_render_osn_actions(receipt))

    _budget = (receipt.recorded_evidence or {}).get("agent_budgets")
    if isinstance(_budget, dict):
        lines.append(f"{_INDENT}BUDGET")
        _limits = _budget.get("limits") or {}
        if _budget.get("enforced"):
            lines.append(_row("Enforced", "yes (agent_budgets)"))
        else:
            lines.append(_row("Enforced", f"no ({_budget.get('reason') or 'unknown'})"))
        if _limits:
            lines.append(_row("Limits", ", ".join(f"{k}={v}" for k, v in _limits.items())))
        _usage = _budget.get("usage") or {}
        if _usage:
            _spend = _usage.get("spend_usd")
            _spend_text = f"${_spend:.4f}" if isinstance(_spend, (int, float)) else "unknown"
            lines.append(_row(
                "Used",
                f"spend {_spend_text}, attempts {_usage.get('attempts')}, "
                f"commands {_usage.get('commands')}, writes {_usage.get('writes')}",
            ))
        if _budget.get("limit_reached"):
            lines.append(_row("Reached", str(_budget["limit_reached"])))
        if _budget.get("action") and _budget["action"] != "none":
            lines.append(_row("Action", str(_budget["action"])))
        lines.append("")

    _routing = (receipt.recorded_evidence or {}).get("adaptive_routing")
    if isinstance(_routing, dict):
        lines.append(f"{_INDENT}ADAPTIVE ROUTING")
        if _routing.get("applied"):
            lines.append(_row("Applied", f"yes: {_routing.get('selected_model')} ({_routing.get('routing_class')})"))
            _ladder = _routing.get("escalation_ladder") or []
            lines.append(_row("Ladder", f"{', '.join(_ladder) if _ladder else 'none'} [{_routing.get('ladder_source')}]"))
        else:
            lines.append(_row("Applied", f"no ({_routing.get('reason') or 'unknown'})"))
        _pol = _routing.get("policy") or {}
        if _pol.get("name"):
            _step = f", step {_routing['step_type']}" if _routing.get("step_type") else ""
            lines.append(_row("Policy", f"{_pol.get('name')}@{_pol.get('version')}{_step}"))
        if _routing.get("promotion_state"):
            lines.append(_row("Promotion", str(_routing["promotion_state"])))
        lines.append(_row("History", str(_routing.get("history_evidence") or "unknown")))
        _shadow = _routing.get("shadow_candidates") or []
        if _shadow:
            lines.append(_row("Shadow", ", ".join(_shadow) + " (discovered; would qualify if promoted)"))
        lines.append("")

    _sup = (receipt.recorded_evidence or {}).get("supervisor_routing")
    if isinstance(_sup, dict):
        lines.append(f"{_INDENT}SUPERVISOR")
        _mode = str(_sup.get("record_mode") or "unknown")
        if _sup.get("not_applied_reason"):
            _mode += f" ({_sup['not_applied_reason']})"
        lines.append(_row("Mode", _mode))
        for _d in _sup.get("decisions") or []:
            _line = f"after attempt {_d.get('attempt')}: {_d.get('action')} ({_d.get('reason')})"
            if _d.get("recommended_model"):
                _line += f" -> {_d['recommended_model']}"
            _rr = (_d.get("evidence") or {}).get("reroute") or {}
            if _rr.get("changed_from_plan"):
                _line += " (re-routed)"
            _line += "" if _d.get("acted_on") else " [not acted on]"
            lines.append(_row("Decision", _line))
        if not _sup.get("decisions"):
            lines.append(_row("Decision", "never consulted"))
        lines.append("")

    _caps = (receipt.recorded_evidence or {}).get("capability_snapshot")
    if isinstance(_caps, dict):
        lines.append(f"{_INDENT}CAPABILITIES")
        _on = sorted(k for k, v in (_caps.get("enabled") or {}).items() if v)
        _src = str(_caps.get("source") or "unknown")
        if _caps.get("reason"):
            _src += f" ({_caps['reason']})"
        lines.append(_row("Source", _src + (", read at run start" if _caps.get("refreshed_at_run_start") else "")))
        lines.append(_row("Enabled", ", ".join(_on) if _on else "none"))
        lines.append("")

    _learn = (receipt.recorded_evidence or {}).get("learning")
    if isinstance(_learn, dict):
        lines.append(f"{_INDENT}LEARNING")
        _lsnap_raw = _learn.get("snapshot")
        _lsnap: dict = _lsnap_raw if isinstance(_lsnap_raw, dict) else {}
        _trim_note = (
            f"{_lsnap.get('signals_stored')} of {_lsnap.get('signals_derived')} derived signal(s) stored "
            "(trimmed to fit)" if _lsnap.get("trimmed") is True else None
        )
        if _learn.get("used"):
            _considered = _learn.get("signals_considered")
            lines.append(_row("Signals", f"{_learn.get('signals_used') or 0} prior verified signal(s) considered"
                              + (f" (of {_considered})" if _considered else "")))
            if _trim_note:
                lines.append(_row("Snapshot", _trim_note))
            _ctx = "supplied to the model" if _learn.get("context_supplied") else "not supplied"
            _files = _learn.get("context_files_added") or []
            lines.append(_row("Context", _ctx + (f" (+ {', '.join(_files)})" if _files else "")))
        else:
            _why = {"disabled": "not consulted (--no-learning)", "no_history": "no prior verified evidence",
                    "no_relevant_signals": "no relevant signals", "error": "history could not be read",
                    "unavailable": "not used: learning evidence could not be read",
                    "timeout": "not used: the bounded learning lookup did not finish in time"}
            _snap = _learn.get("snapshot")
            _snap_why = {"missing": "not used: learning snapshot not built yet",
                         "oversized": "not used: learning snapshot exceeded the size cap"}.get(
                (_snap.get("status") if isinstance(_snap, dict) else None) or "")
            _status = str(_learn.get("status"))
            lines.append(_row("Signals", (_snap_why if _status == "unavailable" else None)
                              or _why.get(_status, _status)))
            if _trim_note and _status == "no_relevant_signals":
                lines.append(_row("Snapshot", _trim_note))
        _rr = f" ({_learn['routing_reason']})" if _learn.get("routing_reason") else ""
        lines.append(_row("Routing", f"influenced: {'yes' if _learn.get('routing_influenced') else 'no'}{_rr}"))
        lines.append(_row("Verification", "influenced: " + ("yes" if _learn.get("verification_influenced")
                                                             else "no (recommendations are advisory)")))
        for _label in _learn.get("recommended_checks") or []:
            lines.append(_row("Suggested", f"`{_label}` (not run)"))
        lines.append("")

    if receipt.execution_spans:
        lines.append(f"{_INDENT}EXECUTION SPANS")
        _es_cap = 10
        for _es in receipt.execution_spans[:_es_cap]:
            _dur = f"  {_es.duration_ms}ms" if _es.duration_ms is not None else ""
            _st = f"  {_es.status}" if _es.status else ""
            _ec_str = f"  [{_es.error_class}]" if _es.error_class else ""
            lines.append(f"{_INDENT}  {_es.kind}  {_es.name}{_st}{_dur}{_ec_str}")
        if len(receipt.execution_spans) > _es_cap:
            lines.append(f"{_INDENT}  +{len(receipt.execution_spans) - _es_cap} more")
        lines.append("")

    lines.append(f"{_INDENT}FINDINGS")
    if receipt.findings:
        # Show substantive findings (no cap in full receipt) grouped by severity.
        visible_sub, meta_group, _, _ = group_review_findings(
            receipt.findings,
            caps={"Critical": 999, "High": 999, "Medium": 999, "Low": 999},
        )
        current_severity: str | None = None
        for finding in visible_sub:
            if finding.severity != current_severity:
                if current_severity is not None:
                    lines.append("")
                lines.append(f"{_INDENT}{finding.severity.upper()}")
                current_severity = finding.severity
            icon = _FINDING_ICONS.get(finding.severity, "-")
            msg = finding.message
            if finding.path:
                loc = f"{finding.path}:{finding.line}" if finding.line is not None else finding.path
                msg = f"{msg}  [{loc}]"
            lines.append(f"{_INDENT}  {icon} {msg}")
        # Metadata section: show all examples (up to 10)
        if meta_group:
            if current_severity is not None:
                lines.append("")
            lines.append(f"{_INDENT}METADATA")
            # Collect all metadata findings for the expanded view
            meta_findings = [f for f in receipt.findings if _is_metadata_noise(f)]
            n = len(meta_findings)
            uses_labels = any("label" in f.message.lower() for f in meta_findings)
            has_gcp     = any("google_" in f.message for f in meta_findings)
            term   = "labels" if uses_labels else "tags"
            prefix = "GCP " if has_gcp else ""
            lines.append(f"{_INDENT}  ~ {n} {prefix}resources missing ownership/environment {term}")
            examples: list[str] = []
            seen_ex: set[str] = set()
            for f in meta_findings:
                parts = f.message.split()
                if len(parts) >= 2 and parts[0].lower() == "resource":
                    name = parts[1]
                    if name not in seen_ex:
                        seen_ex.add(name)
                        examples.append(name)
            for ex in examples[:10]:
                lines.append(f"{_INDENT}    {ex}")
            if len(examples) > 10:
                lines.append(f"{_INDENT}    ...and {len(examples) - 10} more")
    else:
        lines.append(f"{_INDENT}  No structured findings recorded.")
    lines.append("")

    if receipt.model_advisory:
        lines.append(f"{_INDENT}MODEL ADVISORY")
        lines.append(f"{_INDENT}  Advisory only — routing unchanged")
        lines.append(f"{_INDENT}  Generated from risk signal only")
        for _adv in receipt.model_advisory:
            _name = _adv.get("display_name") or _adv.get("model_id", "?")
            lines.append(f"{_INDENT}  {_name}")
            for _r in _adv.get("reasons", [])[:3]:
                lines.append(f"{_INDENT}    ↳ {_r}")
        lines.append("")

    if detail == "full" and receipt.feedback_routing_advisory:
        _fra = receipt.feedback_routing_advisory
        lines.append(f"{_INDENT}FEEDBACK ROUTING ADVISORY")
        lines.append(f"{_INDENT}  Advisory only — routing unchanged")
        _rec = _fra.get("recommendation", "").replace("_", " ")
        lines.append(f"{_INDENT}  Recommendation  {_rec}")
        lines.append(f"{_INDENT}  Confidence      {_fra.get('confidence', '')}")
        _reason = _fra.get("reason", "")
        if _reason:
            lines.append(f"{_INDENT}  Reason          {_reason}")
        _sigs = _fra.get("signals_considered") or {}
        _sig_parts = [f"{k}={v}" for k, v in _sigs.items() if isinstance(v, int) and v > 0]
        if _sig_parts:
            lines.append(f"{_INDENT}  Signals         {', '.join(_sig_parts)}")
        lines.append("")

    lines += [_SEP, f"{_INDENT}RECEIPT"]
    if receipt.receipt_id:
        lines.append(_row("Receipt ID", receipt.receipt_id))
    if receipt.task_id:
        lines.append(_row("Task ID", receipt.task_id))
    lines.append(_row("Shard ID", receipt.shard_id))
    lines.append(_row("Created", _fmt_timestamp(receipt.created_at)))
    lines.append(_row("Integrity", receipt.integrity))
    if receipt.integrity_status != "missing":
        lines.append(_row("", INTEGRITY_NOTE))
    lines.append(_row("Result", receipt.result))
    lines.append(_SEP)

    return "\n".join(lines)
