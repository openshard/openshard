from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from openshard.analysis.repo import RepoFacts, analyze_repo
from openshard.config.settings import load_config
from openshard.models.registry import display_name_for
from openshard.run.pipeline import _LOG_PATH

from .console import make_console
from .theme import ACCENT_STYLE, BORDER_STYLE, BRAND_STYLE, MUTED_STYLE, SECTION_STYLE, TIP_STYLE

TAGLINE = "The control layer for AI coding agents."

QUICK_COMMANDS = [
    ("run",      "Make a controlled change"),
    ("last",     "Inspect latest receipt"),
    ("history",  "Recent work in this repo"),
    ("context",  "What OpenShard would surface for a task"),
    ("stats",    "Counts over recorded Shards"),
    ("models",     "View model route"),
    ("demo shard", "Preview OpenShard output"),
]


@dataclass
class HomeState:
    title: str
    mode: str
    model: str
    repo_short: str
    git_state: str
    stack_state: str
    receipts: list[dict[str, Any]]


def gather_home_state() -> HomeState:
    cwd = Path.cwd()
    config = _safe_load_config()
    facts = _safe_analyze_repo(cwd)
    execution_model = _extract_model(config)
    return HomeState(
        title=_title(),
        mode="Configured" if execution_model else "Not configured",
        model=execution_model or "Not configured",
        repo_short=_repo_short(cwd),
        git_state=_git_state(cwd, facts),
        stack_state=_stack_state(facts),
        receipts=_recent_receipts(cwd / _LOG_PATH),
    )


def render_home_screen() -> None:
    """Render the OpenShard home screen without provider/model side effects."""
    state = gather_home_state()
    console = make_console()
    console.print(_build_cockpit_panel(state))
    console.print()
    console.print(_build_quick_commands_panel())
    console.print()
    console.print(_build_prompt_hint_panel())
    console.print()
    console.print(render_controls_row())
    console.print()


render_home = render_home_screen  # backward-compat alias


def render_brand_panel(state: HomeState) -> Table:
    outer = Table.grid()
    outer.add_column()

    header = Text()
    header.append("* OpenShard", style=BRAND_STYLE)
    outer.add_row(header)
    outer.add_row(Text(TAGLINE, style=MUTED_STYLE))
    outer.add_row(Text(""))

    status = Table.grid(padding=(0, 1))
    status.add_column(style=SECTION_STYLE, no_wrap=True)
    status.add_column()
    status.add_row("Mode:", state.mode)
    status.add_row("Model:", _friendly_model(state.model))
    status.add_row("Repo:", state.repo_short)
    status.add_row("Git:", state.git_state)
    status.add_row("Stack:", state.stack_state)

    outer.add_row(status)
    return outer


def render_tips_panel() -> Text:
    t = Text()
    t.append("Tips for getting started\n", style=ACCENT_STYLE)
    for tip in (
        'openshard run "explain this repo"',
        "openshard last --full",
        "openshard demo shard",
        "openshard models",
    ):
        t.append(f"  > {tip}\n", style=TIP_STYLE)
    return t


def render_recent_receipts(receipts: list[dict[str, Any]]) -> Table:
    outer = Table.grid()
    outer.add_column()
    outer.add_row(Text("Recent receipts", style=ACCENT_STYLE))
    if not receipts:
        outer.add_row(Text("  No recent receipts yet.", style=MUTED_STYLE))
        return outer

    # Fixed columns keep each receipt on one clean row. no_wrap stops "no cost"
    # (and costs) from splitting across lines; min_width protects the Cost/Verify
    # columns so only the Task cell ellipsizes in a narrow panel.
    table = Table.grid(padding=(0, 2))
    table.add_column(style=SECTION_STYLE, no_wrap=True, overflow="ellipsis")  # Task
    table.add_column(style=MUTED_STYLE, no_wrap=True, justify="left", min_width=7)  # Cost
    table.add_column(style=MUTED_STYLE, no_wrap=True, min_width=7)  # Verify
    table.add_row("Task", "Cost", "Verify")
    for entry in reversed(receipts):
        task, cost, verify = _receipt_row(entry)
        table.add_row(task, cost, verify)

    outer.add_row(table)
    return outer


def render_controls_row() -> Text:
    t = Text()
    t.append("OpenShard controls:  ", style=SECTION_STYLE)
    for i, label in enumerate(("Route", "Risk", "Verify", "Cost", "Receipt")):
        if i:
            t.append("  ")
        t.append(label, style=BRAND_STYLE)
    return t


# ── internal helpers ──────────────────────────────────────────────────────────


def _build_cockpit_panel(state: HomeState) -> Panel:
    right_col = Table.grid()
    right_col.add_column()
    right_col.add_row(render_tips_panel())
    right_col.add_row(Text(""))
    right_col.add_row(render_recent_receipts(state.receipts))

    grid = Table.grid(padding=(0, 3), expand=True)
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)
    grid.add_row(render_brand_panel(state), right_col)

    return Panel(
        grid,
        title=state.title,
        title_align="center",
        border_style=BORDER_STYLE,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def _build_quick_commands_panel() -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True, min_width=10)
    grid.add_column(style=MUTED_STYLE)
    for cmd, desc in QUICK_COMMANDS:
        grid.add_row(cmd, desc)
    return Panel(
        grid,
        title="Quick commands",
        title_align="left",
        border_style=BORDER_STYLE,
        box=box.ROUNDED,
        padding=(0, 2),
    )


def _build_prompt_hint_panel() -> Panel:
    t = Text()
    t.append("> ", style=BRAND_STYLE)
    t.append('Type a command or try: openshard run "explain this repo"', style=MUTED_STYLE)
    return Panel(t, border_style=BORDER_STYLE, box=box.ROUNDED, padding=(0, 2))



def _title() -> str:
    try:
        version = metadata.version("openshard")
    except metadata.PackageNotFoundError:
        return "OpenShard dev"
    return f"OpenShard v{version}"


def _safe_load_config() -> dict[str, Any]:
    try:
        return load_config()
    except Exception:
        return {}


def _safe_analyze_repo(path: Path) -> RepoFacts | None:
    try:
        return analyze_repo(path)
    except Exception:
        return None


def _friendly_model(model: str) -> str:
    """Human-friendly model name for the dashboard, e.g. 'Claude Sonnet 4.6'.

    Uses the registry display name and drops the '<Provider>: ' prefix so the
    dashboard reads naturally. Raw model IDs are kept elsewhere (JSON/debug/registry).
    """
    if not model or model == "Not configured":
        return model
    display = display_name_for(model)
    _provider, sep, name = display.partition(": ")
    return name if sep else display


def _extract_model(config: dict[str, Any]) -> str | None:
    if not config:
        return None
    model = config.get("execution_model") or _tier_model(config, "balanced")
    return str(model) if model else None


def _tier_model(config: dict[str, Any], tier_name: str) -> str | None:
    for tier in config.get("model_tiers", []) or []:
        if isinstance(tier, dict) and tier.get("name") == tier_name:
            model = tier.get("model")
            return str(model) if model else None
    return None


def _repo_short(path: Path) -> str:
    parts = path.parts
    if len(parts) >= 2:
        return "/".join(parts[-2:])
    return path.name or str(path)


def _git_state(path: Path, facts: RepoFacts | None) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        result = None

    if result is None or result.returncode != 0:
        changed = len(facts.changed_files) if facts else 0
        if changed:
            return f"{changed} changed file(s)"
        return "Git unavailable"

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    changed = [line for line in lines if not line.startswith("##")]  # type: ignore[assignment]  # reuses name from early-return branch where changed was int
    branch = lines[0].removeprefix("## ").strip() if lines else "unknown branch"
    if changed:
        return f"{branch} · {len(changed)} changed"  # type: ignore[arg-type]  # changed is list[str] here; mypy infers int from earlier branch
    return f"{branch} · clean"


def _stack_state(facts: RepoFacts | None) -> str:
    if facts is None:
        return "repo analysis unavailable"

    parts: list[str] = []
    if facts.languages:
        parts.append(", ".join(lang.title() for lang in facts.languages))
    if facts.framework:
        parts.append(facts.framework)
    if facts.test_command:
        parts.append("tests detected")
    elif facts.package_files:
        parts.append(", ".join(facts.package_files[:3]))
    return " · ".join(parts) if parts else "stack not detected"


def _recent_receipts(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []

    entries: list[dict[str, Any]] = []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries[-3:]


def _receipt_row(entry: dict[str, Any]) -> tuple[str, str, str]:
    """Return (task, cost, verify) cells for one recent-receipt row."""
    task = _shorten(str(entry.get("task") or entry.get("summary") or "untitled run"), 16)
    cost = entry.get("estimated_cost")
    cost_label = f"${cost:.4f}" if isinstance(cost, (int, float)) else "no cost"
    return task, cost_label, _verify_label(entry)


def _verify_label(entry: dict[str, Any]) -> str:
    if entry.get("verification_passed") is True:
        return "verified"
    if entry.get("verification_passed") is False:
        return "failed"
    if entry.get("verification_attempted") is False:
        return "skipped"
    return "none"


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
