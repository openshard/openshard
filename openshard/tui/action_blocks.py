from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openshard.history.shard_contract import FileEvidence, ShardReceipt

_MAX_EVIDENCE = 10
_NOT_RECORDED = frozenset({"Not recorded", "Not run"})

_ROLE_LABELS: dict[str, str] = {
    "inspected": "inspected/read context",
    "finding_source": "finding source",
    "changed": "changed",
}


def render_action_block(title: str, detail: str | None = None) -> str:
    lines = [f"  {title}"]
    if detail:
        lines.append(f"    ↳ [dim]{detail}[/dim]")
    return "\n".join(lines)


def _event_detail(ev: dict) -> str | None:
    if ev.get("detail"):
        return str(ev["detail"])
    count = ev.get("count")
    if count is not None:
        return f"{count} total"
    status = ev.get("status", "completed")
    if status == "skipped":
        return "skipped"
    if status == "failed":
        return "failed"
    return None


def render_actions_section(events: list[dict]) -> str:
    if not events:
        return ""
    lines = ["[bold]ACTIONS[/bold]"]
    for ev in events:
        label = str(ev.get("label") or "").strip()
        if not label:
            continue
        lines.append(render_action_block(label, _event_detail(ev)))
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def render_check_actions_section(checks: list[dict]) -> str:
    if not checks:
        return ""
    lines = ["[bold]CHECK ACTIONS[/bold]"]
    for check in checks:
        name = str(check.get("name") or "").strip()
        if not name:
            continue
        status = check.get("status", "")
        if status == "passed":
            icon = "[green]✓[/green]"
            detail: str | None = check.get("summary") or "passed"
        elif status == "failed":
            icon = "[red]✗[/red]"
            detail = check.get("summary") or "failed"
        elif status == "skipped":
            icon = "[dim]-[/dim]"
            reason = check.get("reason") or ""
            detail = f"skipped — {reason}" if reason else "skipped"
        else:
            icon = " "
            detail = status or None
        lines.append(render_action_block(f"{icon} {name}", detail))
    if len(lines) == 1:
        return ""
    return "\n".join(lines) + "\n"


def render_evidence_section(evidence: list[FileEvidence]) -> str:
    if not evidence:
        return ""
    lines = ["[bold]EVIDENCE[/bold]"]

    shown = evidence[:_MAX_EVIDENCE]
    for fe in shown:
        if len(fe.roles) == 1:
            role = fe.roles[0]
            if role == "inspected":
                title = f"Read {fe.path}"
            elif role == "finding_source":
                title = f"Finding source {fe.path}"
            else:
                title = f"Changed {fe.path}"
            lines.append(render_action_block(title, _ROLE_LABELS[role]))
        else:
            lines.append(f"  {fe.path}")
            for role in fe.roles:
                lines.append(f"    ↳ [dim]{_ROLE_LABELS[role]}[/dim]")
        lines.append("")

    if len(evidence) > _MAX_EVIDENCE:
        lines.append(f"  +{len(evidence) - _MAX_EVIDENCE} more files")
        lines.append("")

    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def _checks_text(receipt: ShardReceipt) -> str | None:
    """The checks line: a later OpenShard re-run or CI verdict wins over the session's own display.

    The same rule `openshard history` rows use (``verification_truth.checks_row_label``),
    so the TUI never shows an agent's claim as current once OpenShard has re-run the checks.
    """
    from openshard.history.verification_truth import checks_row_label, interpret_receipt

    try:
        later = checks_row_label(interpret_receipt(receipt))
    except Exception:
        later = None
    return later or receipt.checks_display


def render_result_section(receipt: ShardReceipt) -> str:
    rows: list[str] = []

    if receipt.result and receipt.result not in _NOT_RECORDED:
        rows.append(render_action_block(receipt.result))

    if receipt.risk and receipt.risk not in _NOT_RECORDED:
        rows.append(render_action_block(f"Risk  {receipt.risk}"))

    if receipt.approval and receipt.approval not in _NOT_RECORDED:
        rows.append(render_action_block(f"Approval  {receipt.approval}"))

    checks = _checks_text(receipt)
    if checks and checks not in _NOT_RECORDED:
        rows.append(render_action_block("Checks", checks))

    if receipt.shard_id:
        rows.append(render_action_block("Receipt", receipt.shard_id))

    if receipt.cost_display and receipt.cost_display not in _NOT_RECORDED:
        rows.append(render_action_block("Cost", receipt.cost_display))

    if not rows:
        return ""
    return "[bold]RESULT[/bold]\n" + "\n".join(rows) + "\n"
