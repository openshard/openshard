from __future__ import annotations

import time
import uuid
from importlib.metadata import version as _pkg_version
from pathlib import Path

from click.testing import CliRunner
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.events import Key
from textual.message import Message
from textual.widgets import Label, Static, TextArea

from openshard.history.session_events import SessionEventWriter, _truncate
from openshard.tui.ask_mode import answer_ask_mode
from openshard.tui.commands import TuiCommand, parse_tui_input
from openshard.tui.plan_mode import answer_plan_mode

_BRAND_ANSI_SHADOW = (
    "██████╗ ██████╗ ███████╗███╗   ██╗███████╗██╗  ██╗ █████╗ ██████╗ ██████╗\n"
    "██╔═══██╗██╔══██╗██╔════╝████╗  ██║██╔════╝██║  ██║██╔══██╗██╔══██╗██╔══██╗\n"
    "██║   ██║██████╔╝█████╗  ██╔██╗ ██║███████╗███████║███████║██████╔╝██║  ██║\n"
    "██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║╚════██║██╔══██║██╔══██║██╔══██╗██║  ██║\n"
    "╚██████╔╝██║     ███████╗██║ ╚████║███████║██║  ██║██║  ██║██║  ██║██████╔╝\n"
    " ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝"
)
_BRAND_FALLBACK = "✦ OpenShard"
_BRAND_MIN_WIDTH = 100  # conservative threshold; avoids Windows Terminal wrapping/clipping

try:
    _VERSION = f"v{_pkg_version('openshard')}"
except Exception:
    _VERSION = "v0.1.0"

TAGLINE = "Receipts for AI coding agents"

_MODE_STRIP_DEFAULT = "Auto mode"
_STATUS_STRIP = "↳ Sandbox [OFF]     ↳ Receipts [ON]     ↳ Checks [AUTO]     ↳ Approval [SMART]"
_SLASH_MENU_TEXT = (
    "  /help                    Show help\n"
    "  /packs                   List workflow packs\n"
    "  /pack <id>               Load a workflow pack\n"
    "  /last                    Show most recent run\n"
    "  /last more               Show more detail\n"
    "  /last full               Show full debug/audit detail\n"
    "  /feedback <outcome>      Record outcome for last run\n"
    "  /ask <question>          Ask a product question\n"
    "  /plan <task>             Generate a local execution plan\n"
    "  /clear                   Clear output\n"
    "  /quit                    Exit\n"
    "\n"
    "  CLI: openshard reflect last\n"
    "  CLI: openshard pr comment"
)

_GIT_STATE_LABELS = {
    "clean": "Clean",
    "dirty": "Changes pending",
    "unknown": "Unknown",
}

_STATUS_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "read-only": "Read-only",
    "unknown": "Unknown",
}

_STATUS_COLORS = {
    "passed": "green",
    "failed": "red",
    "read-only": "dim",
    "unknown": "dim",
}

_HELP_TEXT = (
    "Supported commands:\n"
    "  /help                    Show this help\n"
    "  /last                    Show most recent run\n"
    "  /last more               Show more detail\n"
    "  /last full               Show full debug/audit detail\n"
    "  /feedback <outcome>      Record outcome for last run\n"
    "  /feedback <outcome> <reason>  Record outcome with reason\n"
    "  /clear                   Clear output area\n"
    "  /quit                    Exit the TUI\n"
    "  /packs                   List available workflow packs\n"
    "  /pack <id>               Show a workflow pack prompt\n"
    "  /ask <question>          Ask a product question (no run)\n"
    "  /plan <task>             Generate a local execution plan (no run)\n"
    "  (plain text)             Run as openshard task\n"
    "\n"
    "Composer keys:\n"
    "  Enter           Submit task\n"
    "  Ctrl+J          Insert newline\n"
    "  Shift+Enter     Insert newline (if terminal supports it)\n"
    "\n"
    "CLI commands (run in terminal):\n"
    "  openshard reflect last\n"
    "  openshard pr comment\n"
    "  openshard pr comment --output pr-comment.md"
)


def _render_packs_list() -> str:
    from openshard.workflow_packs.packs import load_packs

    packs = load_packs()
    col = 32
    lines = ["Workflow packs", ""]
    for p in packs:
        lines.append(f"{p.id:<{col}}{p.title}")
    lines += ["", "Use:", "  /pack <pack-id>"]
    return "\n".join(lines)


def _render_pack_detail(pack_id: str | None) -> str:
    from openshard.workflow_packs.packs import get_pack, load_packs

    if pack_id is None:
        ids = "\n".join(f"  {p.id}" for p in load_packs())
        return f"Usage: /pack <pack-id>\n\nAvailable packs:\n{ids}"
    try:
        p = get_pack(pack_id)
    except KeyError:
        ids = "\n".join(f"  {p.id}" for p in load_packs())
        return f'Unknown pack: "{pack_id}"\n\nAvailable packs:\n{ids}'
    return f"Workflow pack selected: {p.title}"


def _render_user_block(text: str) -> str:
    # v1: styled label + guide line; v2 will add action/file/check sub-blocks
    rail = "[blue]│[/blue]"
    lines = text.split("\n")
    header = f"{rail}  [bold]YOU[/bold]"
    body = "\n".join(f"{rail}  {line}" for line in lines)
    return f"{header}\n{body}\n"


def _render_openshard_block(text: str) -> str:
    # v1: styled label only; body unchanged to preserve receipt alignment
    return f"[dark_orange]OPENSHARD[/dark_orange]\n{text}\n"


def _extract_receipt_block(output: str) -> str | None:
    """Return the receipt block from CLI output, or None if not found.

    Tries two formats in order:
    1. Separator receipt: a line where the stripped text starts with "RECEIPT"
       and contains "—" or "-" (e.g. "RECEIPT — shard-20260520-0001").
       Walks back one line to include the preceding separator rule.
    2. Rich box receipt: a line containing "╭─ OpenShard Receipt".
    """
    lines = output.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("RECEIPT") and ("—" in stripped or "-" in stripped):
            return "\n".join(lines[max(0, i - 1):])

    for i, line in enumerate(lines):
        if "╭─ OpenShard Receipt" in line:
            return "\n".join(lines[i:])

    return None


_REVIEW_MEMO_STARTERS = frozenset({"Review complete", "OpenShard completed the review"})

_HIDDEN_SENTINELS = (
    "\n\nAfter completing your analysis",
    "STRUCTURED_FINDINGS",
)


def _sanitize_hidden_contract(text: str) -> str:
    for sentinel in _HIDDEN_SENTINELS:
        idx = text.find(sentinel)
        if idx != -1:
            text = text[:idx].rstrip()
    return text


def _safe_argv_preview(args: list[str]) -> str:
    parts = [_sanitize_hidden_contract(str(a)) for a in args]
    preview = " ".join(parts)
    if len(preview) > 160:
        preview = preview[:157] + "…"
    return preview


def _extract_run_display(output: str) -> str:
    """Return the portion of CLI run output to show in the TUI.

    For review runs: preserves the memo block + receipt that follows.
    For normal runs with a receipt: receipt block only.
    Fallback: last 30 lines.
    """
    lines = output.splitlines()

    receipt_start: int | None = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("RECEIPT") and ("—" in stripped or "-" in stripped):
            receipt_start = max(0, i - 1)
            break
    if receipt_start is None:
        for i, line in enumerate(lines):
            if "╭─ OpenShard Receipt" in line:
                receipt_start = i
                break

    scan_limit = receipt_start if receipt_start is not None else len(lines)
    for i in range(scan_limit):
        stripped = lines[i].strip()
        if (
            any(stripped.startswith(m) for m in _REVIEW_MEMO_STARTERS)
            or (stripped.startswith("Found ") and "issues" in stripped)
        ):
            return "\n".join(lines[i:])

    if receipt_start is not None:
        return "\n".join(lines[receipt_start:])

    return "\n".join(lines[-30:])


_TL_SYMBOLS = frozenset("✓✖→-+x>")


def _is_timeline_row(line: str) -> bool:
    """True when line is an indented CLI timeline row: '  {symbol} {label}'."""
    return len(line) >= 4 and line[:2] == "  " and line[2] in _TL_SYMBOLS and line[3] == " "


def _strip_run_timeline_block(text: str) -> str:
    """Remove the 'Running …' live-checklist block from run display text.

    The block is emitted by render_run_timeline() after the review memo and
    duplicates the structured ACTIONS summary.  Only strips when the line
    matches the exact CLI format: a bare 'Running …' header immediately
    followed by a blank line, then indented symbol rows ('  ✓/✖/→/- label').
    Conservative — does not touch lines that don't fit this pattern.
    """
    lines = text.splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Running "):
            # Peek ahead past blank lines to see if timeline rows follow
            j = i + 1
            while j < len(lines) and lines[j] == "":
                j += 1
            if j < len(lines) and _is_timeline_row(lines[j]):
                # Confirmed: skip "Running …", blank lines, timeline rows, trailing blanks
                while j < len(lines) and (lines[j] == "" or _is_timeline_row(lines[j])):
                    j += 1
                # Drop the blank line before "Running …" that we already added, if present
                if result and result[-1] == "":
                    result.pop()
                i = j
                continue
        result.append(line)
        i += 1
    return "\n".join(result)


class TaskInput(TextArea):
    """Multi-line task composer. Enter submits; Ctrl+J / Shift+Enter insert newline."""

    BORDER_TITLE = "Type a task or / for commands"

    class Submit(Message):
        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    def on_key(self, event: Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            self.post_message(self.Submit(self.text))
        elif event.key in ("ctrl+j", "shift+enter"):
            event.prevent_default()
            self.insert("\n")


class OpenShardTui(App):
    CSS_PATH = "styles.tcss"

    def __init__(self, path: Path | None = None) -> None:
        super().__init__()
        self._path = path or Path.cwd()
        self._git_info: dict = {}
        self._recent_runs: list[dict] | None = None
        self._guardrails: dict = {}
        self._output_lines: list[str] = []
        self._run_in_progress: bool = False
        self._run_start: float = 0.0
        self._pack_suffix: str = ""
        self._pack_workflow: str = ""
        self._session_id: str = str(uuid.uuid4())
        self._session_writer: SessionEventWriter = SessionEventWriter(base_path=self._path)

    def compose(self) -> ComposeResult:
        with Horizontal(id="header-card"):
            with Vertical(id="header-left"):
                yield Static("", id="header-brand")
                yield Static(TAGLINE, id="header-tagline")
                yield Static("", id="header-project")
            yield Static(_VERSION, id="header-version")
        yield ScrollableContainer(Static("", id="output-content"), id="output-panel")
        with Horizontal(id="mode-strip"):
            yield Static(_MODE_STRIP_DEFAULT, id="mode-strip-left")
            yield Static("Agent: OpenShard Native (OSN)", id="mode-strip-right")
        yield Static(_SLASH_MENU_TEXT, id="slash-menu")
        yield TaskInput(id="task-input")
        yield Static(_STATUS_STRIP, id="status-strip", markup=False)
        yield Label("", id="status-msg")

    def on_mount(self) -> None:
        from openshard.tui.state import get_guardrails, load_git_info, load_recent_runs

        if not self._git_info:
            self._git_info = load_git_info(self._path)
        if not self._guardrails:
            self._guardrails = get_guardrails()
        if self._recent_runs is None:
            self._recent_runs = load_recent_runs(self._path)

        self._refresh_widgets()
        self._update_brand()
        self._session_writer.write("session_started", self._session_id, summary="TUI session started")

        from openshard.cli.ui.onboarding import _should_run_onboarding

        if _should_run_onboarding():
            from openshard.tui.onboarding_screen import OnboardingScreen

            self.push_screen(OnboardingScreen())

    def on_resize(self, event) -> None:
        self._update_brand()

    def _update_brand(self) -> None:
        brand = _BRAND_ANSI_SHADOW if self.size.width >= _BRAND_MIN_WIDTH else _BRAND_FALLBACK
        self.query_one("#header-brand", Static).update(brand)

    def _refresh_widgets(self) -> None:
        gi = self._git_info
        state_key = gi.get("state", "unknown")
        state_label = _GIT_STATE_LABELS.get(state_key, "Unknown")
        col = 26
        _repo_color = {"clean": "green", "dirty": "yellow", "unknown": "dim"}
        color = _repo_color.get(state_key, "dim")
        pname = gi.get("project_name", "unknown")
        branch = gi.get("branch", "unknown")
        labels = f"[dim]{'PROJECT':<{col}}{'BRANCH':<{col}}REPO[/dim]"
        values = (
            f"[white]{pname:<{col}}[/white]"
            f"[white]{branch:<{col}}[/white]"
            f"[{color}]{state_label}[/{color}]"
        )
        self.query_one("#header-project", Static).update(f"{labels}\n{values}")

    @on(TextArea.Changed, "#task-input")
    def _on_task_input_changed(self, event: TextArea.Changed) -> None:
        text = event.text_area.text
        self.query_one("#slash-menu").display = text.startswith("/") and " " not in text

    def on_task_input_submit(self, event: TaskInput.Submit) -> None:
        raw = event.text.strip()
        if not raw:
            return
        self.query_one("#task-input", TaskInput).load_text("")
        parsed = parse_tui_input(raw)

        _no_echo = {TuiCommand.CLEAR, TuiCommand.QUIT}
        if parsed.cmd not in _no_echo:
            self._append_output(_render_user_block(raw))

        if parsed.cmd == TuiCommand.RUN_TASK:
            self._session_writer.write(
                "user_message", self._session_id, summary=_truncate(raw, 300)
            )
        elif parsed.cmd == TuiCommand.FEEDBACK:
            self._session_writer.write(
                "feedback_recorded",
                self._session_id,
                command=raw.strip(),
                metadata={"outcome": parsed.feedback_outcome},
            )
        else:
            self._session_writer.write(
                "command_invoked",
                self._session_id,
                command=raw.strip(),
                summary=parsed.cmd.name.lower(),
            )

        if parsed.cmd == TuiCommand.HELP:
            self._append_output(_render_openshard_block(_HELP_TEXT))
        elif parsed.cmd == TuiCommand.CLEAR:
            self._pack_suffix = ""
            self._pack_workflow = ""
            self._clear_output()
        elif parsed.cmd == TuiCommand.QUIT:
            self.exit()
        elif parsed.cmd == TuiCommand.LAST:
            self._run_cli_async(["last"])
        elif parsed.cmd == TuiCommand.LAST_MORE:
            self._run_cli_async(["last", "--more"])
        elif parsed.cmd == TuiCommand.LAST_FULL:
            self._run_cli_async(["last", "--full"])
        elif parsed.cmd == TuiCommand.FEEDBACK:
            _fb_args = ["feedback", "--outcome", parsed.feedback_outcome]
            if parsed.feedback_reason:
                _fb_args += ["--reason", parsed.feedback_reason]
            self._run_cli_async(_fb_args)
        elif parsed.cmd == TuiCommand.RUN_TASK:
            self._start_run_status(raw)
            _suffix = self._pack_suffix
            _pw = self._pack_workflow
            self._pack_suffix = ""
            self._pack_workflow = ""
            _effective_task = (parsed.task or "") + _suffix if _suffix else parsed.task  # type: ignore[operator]  # parsed.task may be None; guarded by _suffix check
            _args = ["run", "--workflow", _pw, _effective_task] if _pw else ["run", _effective_task]
            self._run_cli_async(_args, refresh_after=True, is_run=True)
        elif parsed.cmd == TuiCommand.PACKS:
            self._append_output(_render_openshard_block(_render_packs_list()))
        elif parsed.cmd == TuiCommand.PACK_SHOW:
            self._append_output(_render_openshard_block(_render_pack_detail(parsed.pack_id)))
            if parsed.pack_id:
                try:
                    from openshard.workflow_packs.packs import get_pack
                    p = get_pack(parsed.pack_id)
                    self._pack_suffix = p.execution_prompt_suffix
                    self._pack_workflow = p.workflow
                    self.query_one("#task-input", TaskInput).load_text(p.prompt)
                except KeyError:
                    self._pack_suffix = ""
                    self._pack_workflow = ""
        elif parsed.cmd == TuiCommand.ASK:
            answer = answer_ask_mode(parsed.question or "")
            self._append_output(_render_openshard_block(answer))
        elif parsed.cmd == TuiCommand.PLAN:
            answer = answer_plan_mode(parsed.task or "", path=self._path)
            self._append_output(_render_openshard_block(answer))
        else:
            self._append_output(_render_openshard_block(f"Unknown command: {raw}\nType /help for supported commands."))

    def _start_run_status(self, task_text: str) -> None:
        self._run_in_progress = True
        self._run_start = time.monotonic()
        short = task_text[:60].rstrip()
        if len(task_text) > 60:
            short += "…"
        self.query_one("#mode-strip-left", Static).update(
            f"[dim]  ● Executing: {short}  (0s)[/dim]"
        )
        self.set_timer(1.0, self._tick_run_status)

    def _tick_run_status(self) -> None:
        if not self._run_in_progress:
            return
        elapsed = int(time.monotonic() - self._run_start)
        try:
            self.query_one("#mode-strip-left", Static).update(
                f"[dim]  ● Executing  {elapsed}s  — /last more for full output[/dim]"
            )
        except Exception:
            return
        self.set_timer(1.0, self._tick_run_status)

    @work(thread=True)
    def _run_cli_async(
        self, args: list[str], refresh_after: bool = False, is_run: bool = False
    ) -> None:
        # Temporary v0 bridge: routes TUI input through existing Click commands via
        # CliRunner so we avoid shell=True and stay within supported code paths.
        from openshard.cli.main import cli as openshard_cli

        runner = CliRunner()
        result = runner.invoke(openshard_cli, args, input="", catch_exceptions=True)
        raw_output = result.output or ""
        exc_str = str(result.exception) if result.exception else ""
        if not raw_output and exc_str:
            raw_output = exc_str

        if result.exit_code == 0:
            status = "Done."
            output = raw_output
        else:
            status = f"Failed (exit {result.exit_code})."
            _argv_preview = _safe_argv_preview(args)
            _sanitized_output = _sanitize_hidden_contract(raw_output)
            _tail = "\n".join(_sanitized_output.splitlines()[-20:]) if _sanitized_output else "(no output)"
            _sanitized_exc = _sanitize_hidden_contract(exc_str) if exc_str else ""
            _parts = [
                f"[argv] {_argv_preview}",
                f"[exit] {result.exit_code}",
                "[output tail]",
                _tail,
            ]
            if _sanitized_exc:
                _parts.append(f"[exception] {_sanitized_exc}")
            output = "\n".join(_parts)

        self.call_from_thread(self._on_cli_result, output, status, refresh_after, is_run, result.exit_code)

    def _on_cli_result(
        self, output: str, status: str, refresh_after: bool, is_run: bool = False, exit_code: int = 0
    ) -> None:
        self._run_in_progress = False
        self.query_one("#mode-strip-left", Static).update(_MODE_STRIP_DEFAULT)

        failed = status.startswith("Failed")
        _session_run_id: str | None = None
        _session_shard_id: str | None = None

        if is_run and not failed:
            from openshard.tui.action_blocks import (
                render_actions_section,
                render_check_actions_section,
                render_evidence_section,
                render_result_section,
            )
            from openshard.tui.state import load_last_run_entry

            _entry = load_last_run_entry(self._path)
            _action_parts: list[str] = []
            if _entry:
                from openshard.history.shard_contract import build_shard_receipt
                _receipt = build_shard_receipt(_entry)
                _session_run_id = _entry.get("timestamp")
                _session_shard_id = _receipt.shard_id
                _result = render_result_section(_receipt)
                _actions = render_actions_section(_entry.get("run_timeline") or [])
                _check_actions = render_check_actions_section(_entry.get("review_checks") or [])
                _evidence = render_evidence_section(_receipt.file_evidence)
                for _part in (_result, _actions, _check_actions, _evidence):
                    if _part:
                        _action_parts.append(_part)
            _action_summary = "\n".join(_action_parts)

            _raw_display = _extract_run_display(output)
            if _action_summary:
                _raw_display = _strip_run_timeline_block(_raw_display)
            _run_display = _raw_display.rstrip("\n") + "\n" + status
            display = ("\n" + _action_summary + "\n" + _run_display) if _action_summary else _run_display
        else:
            display = output.rstrip("\n") + "\n" + status

        self._append_output(_render_openshard_block(display))

        self._session_writer.write(
            "openshard_response",
            self._session_id,
            run_id=_session_run_id,
            shard_id=_session_shard_id,
            summary="OpenShard response completed",
            metadata={"exit_code": exit_code, "is_run": is_run},
        )

        if refresh_after:
            from openshard.tui.state import load_recent_runs

            self._recent_runs = load_recent_runs(self._path)
            self._refresh_widgets()

    def _append_output(self, text: str) -> None:
        self._output_lines.append(text)
        self.query_one("#output-content", Static).update("\n".join(self._output_lines))
        self.query_one("#output-panel", ScrollableContainer).scroll_end(animate=False)

    def _clear_output(self) -> None:
        self._output_lines.clear()
        self.query_one("#output-content", Static).update("")
