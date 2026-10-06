"""``openshard usage``: a Receipt's token and cost evidence, and Cursor reconciliation."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import click

_EXIT_REFUSED = 1


def _history() -> tuple[Path, list[dict]]:
    from openshard.history.locate import locate_history
    from openshard.history.store import load_history

    loc = locate_history(with_identity=False)
    return loc.root, load_history(loc.runs_path, coerce=False)


def _select(entries: list[dict], ref: str | None) -> tuple[int, dict] | None:
    if not entries:
        return None
    if not ref:
        return len(entries) - 1, entries[-1]
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if ref in (e.get("receipt_id"), e.get("shard_id"), e.get("run_id")):
            return i, e
    return None


def _read_json(path: str) -> object:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise click.ClickException(f"{path} is not JSON") from exc


def _fmt_count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else "unknown"


def _dimension_note(block: dict[str, Any]) -> str:
    parts = [p for p in (block.get("status"), block.get("source"), block.get("surface")) if p]
    if block.get("complete") is False:
        parts.append("incomplete")
    return ", ".join(parts)


def render_usage(receipt_id: str | None, usage: dict[str, Any]) -> list[str]:
    from openshard.history.usage_evidence import usage_line

    tokens = usage.get("tokens") or {}
    cost = usage.get("cost") or {}
    model = usage.get("model") or {}
    lines = [f"Receipt  {receipt_id or 'not recorded'}", f"Usage    {usage_line(usage)}"]
    lines.append(f"Agent    {usage.get('agent') or 'unknown'}")
    if model.get("id"):
        lines.append(f"Model    {model['id']} ({model.get('source') or 'source not recorded'})")
    elif model.get("models"):
        lines.append(f"Models   {', '.join(model['models'])} (no single model)")
    else:
        lines.append("Model    unknown")
    if tokens.get("total") is not None:
        counts = " · ".join(
            f"{label} {_fmt_count(tokens.get(key))}"
            for key, label in (("input", "input"), ("output", "output"), ("cache_read", "cache read"),
                               ("cache_write", "cache write"), ("reasoning", "reasoning"), ("other", "other"))
            if tokens.get(key) is not None
        )
        lines.append(f"Tokens   {_fmt_count(tokens['total'])} total: {counts} [{_dimension_note(tokens)}]")
    else:
        lines.append(f"Tokens   unknown [{tokens.get('status') or 'unknown'}]")
    if cost.get("usd") is not None:
        line = f"Cost     ${cost['usd']:.4f} [{_dimension_note(cost)}]"
        if cost.get("model_cost_usd") is not None or cost.get("platform_fee_usd") is not None:
            line += (f" model ${cost.get('model_cost_usd') or 0:.4f}" if cost.get("model_cost_usd") is not None else "")
            line += (f" + Cursor Token Rate ${cost['platform_fee_usd']:.4f}"
                     if cost.get("platform_fee_usd") else "")
        lines.append(line)
        rate = cost.get("rate")
        if isinstance(rate, dict):
            lines.append(
                f"Rate     {rate.get('provider')}/{rate.get('model_id')} list rate as of {rate.get('pricing_version')}: "
                f"${rate.get('input_per_mtok')}/M input, ${rate.get('output_per_mtok')}/M output "
                "(an estimate, not a bill)"
            )
    else:
        lines.append(f"Cost     unknown [{cost.get('status') or 'unknown'}]")
    for item in usage.get("reconciled_by") or []:
        lines.append(f"Later    {item.get('attestation_id')} at {item.get('created_at')} from {item.get('surface')}")
    return lines


@click.group("usage")
def usage_group() -> None:
    """Token and cost evidence for a Receipt, and Cursor usage reconciliation."""


@usage_group.command("show")
@click.option("--receipt", "receipt_ref", default=None, metavar="ID", help="receipt_id, shard_id or run_id (default: latest).")
@click.option("--json", "as_json", is_flag=True, default=False)
def usage_show(receipt_ref: str | None, as_json: bool) -> None:
    """What this Receipt's tokens and cost rest on: observed, reconciled, estimated or unknown."""
    from openshard.history.usage_evidence import (
        effective_usage,
        load_usage_attestations,
        usage_attestations_for_entry,
    )

    root, entries = _history()
    picked = _select(entries, receipt_ref)
    if picked is None:
        raise click.ClickException("no matching Receipt in this repository's history")
    _, entry = picked
    atts = usage_attestations_for_entry(entry, load_usage_attestations(root / ".openshard"))
    usage = effective_usage(entry, atts)
    rid = entry.get("receipt_id") if isinstance(entry.get("receipt_id"), str) else None
    if as_json:
        click.echo(json.dumps({"receipt_id": rid, "usage": usage}, indent=2))
        return
    for line in render_usage(rid, usage):
        click.echo(line)


@usage_group.group("reconcile")
def usage_reconcile_group() -> None:
    """Attach usage Cursor reported later to the Receipt that carries the same Cursor id."""


def _finish(ctx: click.Context, result: Any, as_json: bool) -> None:
    from openshard.adapters.cursor_usage import OUTCOME_PENDING, OUTCOME_RECORDED, OUTCOME_UNCHANGED

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2))
    elif result.outcome == OUTCOME_RECORDED:
        click.echo(f"Recorded Cursor usage for Receipt {result.receipt_id}.")
    elif result.outcome == OUTCOME_UNCHANGED:
        click.echo(f"Already recorded for Receipt {result.receipt_id}; nothing new.")
    elif result.outcome == OUTCOME_PENDING:
        click.echo(f"Pending: {result.detail}. Nothing recorded; usage stays unknown. Try again later.")
    else:
        click.echo(f"Not recorded ({result.outcome}): {result.detail}. Usage stays unknown.")
    if result.outcome not in (OUTCOME_RECORDED, OUTCOME_UNCHANGED, OUTCOME_PENDING):
        ctx.exit(_EXIT_REFUSED)


def _api_key(env_name: str) -> str:
    key = os.environ.get(env_name, "").strip()
    if not key:
        raise click.ClickException(f"set {env_name} (or pass --from-file with a saved API response)")
    return key


@usage_reconcile_group.command("cursor-agent")
@click.argument("agent_id")
@click.option("--run-id", default=None, metavar="RUN", help="Only this run of the agent (run-...).")
@click.option("--from-file", "from_file", default=None, metavar="PATH",
              help="A saved GET /v1/agents/{id}/usage response ('-' for stdin) instead of calling Cursor.")
@click.option("--receipt", "receipt_ref", default=None, metavar="ID",
              help="Refuse unless this Receipt is the one carrying AGENT_ID.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def reconcile_cursor_agent(
    ctx: click.Context, agent_id: str, run_id: str | None, from_file: str | None,
    receipt_ref: str | None, as_json: bool,
) -> None:
    """Cloud Agents API usage (tokens) for AGENT_ID (bc-...), onto the Receipt captured for it.

    Reads CURSOR_API_KEY. Cursor reports zeros for a run with no recorded
    usage yet; that is reported as pending and nothing is recorded.
    """
    from openshard.adapters.cursor_usage import (
        ENV_API_KEY,
        fetch_agent_usage,
        reconcile_agent_usage,
    )

    root, entries = _history()
    if from_file:
        body = _read_json(from_file)
    else:
        body, err = fetch_agent_usage(agent_id, _api_key(ENV_API_KEY), run_id=run_id)
        if body is None:
            raise click.ClickException(err)
    result = reconcile_agent_usage(root, entries, agent_id, body, run_id=run_id, receipt_ref=receipt_ref)
    _finish(ctx, result, as_json)


@usage_reconcile_group.command("cursor-events")
@click.option("--receipt", "receipt_ref", default=None, metavar="ID", help="receipt_id, shard_id or run_id (default: latest).")
@click.option("--from-file", "from_file", default=None, metavar="PATH",
              help="Saved POST /teams/filtered-usage-events response page(s) ('-' for stdin) instead of calling Cursor.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def reconcile_cursor_events(
    ctx: click.Context, receipt_ref: str | None, from_file: str | None, as_json: bool,
) -> None:
    """Admin API usage events (tokens, model, charged cost) carrying this Receipt's Cursor id.

    Reads CURSOR_ADMIN_API_KEY. Only events whose conversationId /
    cloudAgentId equal the id the capture observed are used, within an hour
    of the Receipt's capture window.
    """
    from openshard.adapters.cursor_usage import (
        ENV_ADMIN_API_KEY,
        EVENT_WINDOW_SLACK_MS,
        KEY_CLOUD_AGENT,
        fetch_usage_events,
        receipt_keys,
        receipt_window_ms,
        reconcile_usage_events,
    )

    root, entries = _history()
    picked = _select(entries, receipt_ref)
    if picked is None:
        raise click.ClickException("no matching Receipt in this repository's history")
    _, entry = picked
    body: object
    if from_file:
        body = _read_json(from_file)
    else:
        start, end = receipt_window_ms(entry)
        if start is None or end is None:
            raise click.ClickException("this Receipt has no capture window to query Cursor for")
        agent = receipt_keys(entry).get(KEY_CLOUD_AGENT, (None, None))[0]
        pages, err = fetch_usage_events(
            _api_key(ENV_ADMIN_API_KEY), start_ms=start - EVENT_WINDOW_SLACK_MS, end_ms=end + EVENT_WINDOW_SLACK_MS,
            cloud_agent_id=agent,
        )
        if pages is None:
            raise click.ClickException(err)
        body = pages
    result = reconcile_usage_events(root, entries, entry, body)
    _finish(ctx, result, as_json)
