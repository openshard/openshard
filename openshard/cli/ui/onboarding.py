"""Interactive first-run onboarding flow for human TTY users.

Run via ``run_onboarding_flow()``.  The Textual app blocks until the user
completes all screens or skips (Escape / ctrl+q).  After the app exits,
selections are written to config and control returns to the caller.

Non-interactive / agent / CI contexts: call ``_should_run_onboarding()`` first;
if it returns False do not call this module at all.

Textual is imported lazily inside the class factories so that importing this
module in non-interactive contexts (tests, CI) does not trigger Textual
initialisation.
"""
from __future__ import annotations

import datetime
import sys
from typing import Any

from openshard.onboarding.choices import (
    DIRECT_PROVIDER_CHOICES,
    EXECUTOR_CHOICES,
    LEGACY_EXECUTOR_LABELS,
    LOCAL_FIRST_NOTICE,
    NEXT_COMMANDS,
    PROVIDER_ROUTE_CHOICES,
    SAFETY_PROFILE_CHOICES,
    USER_TYPE_CHOICES,
    telemetry_summary_line,
)


def _should_run_onboarding() -> bool:
    """Return True only when interactive onboarding should be shown.

    All three conditions must hold:
    - user has not completed onboarding before
    - not running in an explicit agent / CI environment
    - both stdin and stdout are real TTYs

    Note: NO_COLOR alone must NOT skip onboarding — it is a human colour
    preference, not an agent signal — so this uses ``is_ci_or_agent_environment``
    rather than the broader ``is_agent_environment``.
    """
    from openshard.config.onboarding import is_first_run
    from openshard.config.settings import is_ci_or_agent_environment

    if not is_first_run():
        return False
    if is_ci_or_agent_environment():
        return False
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return False
    return True


def _marker_prompt(label: str, highlighted: bool) -> str:
    """Render an option row with a Claude Code-style '>' marker when highlighted."""
    return f"> {label}" if highlighted else f"  {label}"


# ---------------------------------------------------------------------------
# Textual app classes — built inside factories to defer the textual import
# ---------------------------------------------------------------------------

def _build_select_app_class():
    """Return _SelectScreen class (imports Textual on first call)."""
    from textual import on
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import OptionList, Static

    class _SelectScreen(App):
        """Single-question arrow-key selection app using OptionList.

        ↑↓ navigate (OptionHighlighted), Enter confirm (OptionSelected),
        Esc skip the whole flow.
        """

        BINDINGS = [
            Binding("escape", "skip", "Skip setup", show=True),
            Binding("ctrl+q", "skip", "Skip setup", show=False),
        ]

        CSS = """
        Screen {
            background: #0E0F11;
            align: center middle;
        }
        #question {
            text-style: bold;
            color: #E8E8E8;
            margin: 1 2;
        }
        OptionList {
            margin: 0 2;
            background: #0E0F11;
            border: none;
            color: #CCCCCC;
        }
        OptionList > .option-list--option-highlighted {
            background: #1A1B1E;
            color: #E8E8E8;
            text-style: bold;
        }
        #note {
            color: #888888;
            margin: 0 2 0 4;
            min-height: 1;
        }
        #footer {
            color: #555555;
            margin: 1 2;
        }
        """

        def __init__(
            self,
            question: str,
            choices: list[tuple[str, str, str, bool]],
        ) -> None:
            super().__init__()
            self._question = question
            self._choices = choices
            self.selected_value: str | None = None
            self.skipped: bool = False

        def compose(self) -> ComposeResult:
            yield Static(self._question, id="question")
            options = [
                _marker_prompt(label, i == 0)
                for i, (label, _v, _n, _p) in enumerate(self._choices)
            ]
            yield OptionList(*options, id="choice-set")
            yield Static("", id="note")
            yield Static("↑↓ navigate   Enter confirm   Esc skip setup", id="footer")

        def on_mount(self) -> None:
            self.query_one("#choice-set", OptionList).focus()
            self._refresh_note(0)

        def _refresh_markers(self, highlighted: int) -> None:
            ol = self.query_one("#choice-set", OptionList)
            for i, (label, _v, _n, _p) in enumerate(self._choices):
                ol.replace_option_prompt_at_index(i, _marker_prompt(label, i == highlighted))

        def _refresh_note(self, index: int) -> None:
            _label, _value, note, _planned = self._choices[index]
            self.query_one("#note", Static).update(f"↳ {note}" if note else "")

        @on(OptionList.OptionHighlighted, "#choice-set")
        def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
            self._refresh_markers(event.option_index)
            self._refresh_note(event.option_index)

        @on(OptionList.OptionSelected, "#choice-set")
        def _on_selected(self, event: OptionList.OptionSelected) -> None:
            _label, value, _note, _planned = self._choices[event.option_index]
            self.selected_value = value
            self.exit()

        def action_skip(self) -> None:
            self.skipped = True
            self.exit()

    return _SelectScreen


def _build_info_app_class():
    """Return _InfoScreen class (imports Textual on first call)."""
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.widgets import Static

    class _InfoScreen(App):
        """Read-only information screen; Enter continues, Esc skips."""

        BINDINGS = [
            Binding("enter", "continue", "Continue", show=True),
            Binding("escape", "skip", "Skip setup", show=False),
            Binding("ctrl+q", "skip", "Skip setup", show=False),
        ]

        CSS = """
        Screen {
            background: #0E0F11;
            align: center middle;
        }
        #body {
            color: #CCCCCC;
            margin: 1 2;
        }
        #footer {
            color: #555555;
            margin: 1 2;
        }
        """

        def __init__(self, body: str, footer: str = "Enter continue   Esc skip setup") -> None:
            super().__init__()
            self._body = body
            self._footer = footer
            self.skipped: bool = False

        def compose(self) -> ComposeResult:
            yield Static(self._body, id="body")
            yield Static(self._footer, id="footer")

        def action_continue(self) -> None:
            self.exit()

        def action_skip(self) -> None:
            self.skipped = True
            self.exit()

    return _InfoScreen


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _build_key_status_body() -> str:
    from openshard.config.onboarding import ALL_API_KEY_ENV_VARS, api_key_present

    present = api_key_present()
    lines = ["API keys detected:\n"]
    for provider, env_var in ALL_API_KEY_ENV_VARS.items():
        mark = "✓ set" if present.get(provider) else "✗ not set"
        lines.append(f"  {env_var:<28} {mark}")
    lines += [
        "",
        "API keys stay in your environment variables.",
        "OpenShard does not write keys to config.",
    ]
    return "\n".join(lines)


_INJECTED_KEY_FIELDS = ("openrouter_api_key", "anthropic_api_key", "openai_api_key")


def _write_onboarding_config(result: dict[str, Any]) -> None:
    from openshard.config.settings import load_config_safe, save_config

    base, _valid, _path = load_config_safe()
    # Strip API keys injected from env — they must never be written to disk.
    for _k in _INJECTED_KEY_FIELDS:
        base.pop(_k, None)
    onboarding_block: dict[str, Any] = {
        "schema_version": 1,
        "completed_at": datetime.datetime.now(datetime.UTC).isoformat(),
        "skipped": result.get("skipped", False),
    }
    for field in ("user_type", "executor", "provider_route", "provider", "safety_profile"):
        if result.get(field) is not None:
            onboarding_block[field] = result[field]
    base["onboarding"] = onboarding_block
    save_config(base)


def _record_telemetry_notice_seen() -> None:
    """The local-first / telemetry notice was shown and accepted. Never raises."""
    try:
        from openshard.telemetry.state import consent_after_notice

        consent_after_notice(source="onboarding")
    except Exception:
        pass


def run_onboarding_flow() -> None:
    """Run the full interactive onboarding and write results to config.

    Blocks until completion or skip.  Safe to call only when
    ``_should_run_onboarding()`` returns True.
    """
    _SelectScreen = _build_select_app_class()
    _InfoScreen = _build_info_app_class()

    result: dict[str, Any] = {"skipped": False}

    def _run_select(question: str, choices: list[tuple[str, str, str, bool]]) -> str | None:
        app = _SelectScreen(question, choices)
        app.run()
        if app.skipped:
            result["skipped"] = True
        return app.selected_value

    def _run_info(body: str, footer: str = "Enter continue   Esc skip setup") -> bool:
        app = _InfoScreen(body, footer)
        app.run()
        if app.skipped:
            result["skipped"] = True
        return app.skipped

    # Screen 1
    val = _run_select("Who is using OpenShard?", USER_TYPE_CHOICES)
    if val is not None:
        result["user_type"] = val
    if result["skipped"]:
        _write_onboarding_config(result)
        return

    # Screen 2
    val = _run_select("How do you want to use OpenShard?", EXECUTOR_CHOICES)
    if val is not None:
        result["executor"] = val
    if result["skipped"]:
        _write_onboarding_config(result)
        return

    # Screen 3a
    val = _run_select("How do you want OpenShard to access models?", PROVIDER_ROUTE_CHOICES)
    if val is not None:
        result["provider_route"] = val
        if val == "openrouter":
            result["provider"] = "openrouter"
        elif val == "demo":
            result["provider"] = "skip"
    if result["skipped"]:
        _write_onboarding_config(result)
        return

    # Screen 3b — only if direct
    if result.get("provider_route") == "direct":
        val = _run_select("Choose direct provider:", DIRECT_PROVIDER_CHOICES)
        if val is not None:
            result["provider"] = val
        if result["skipped"]:
            _write_onboarding_config(result)
            return

    # Screen 4 — key status
    if _run_info(_build_key_status_body()):
        _write_onboarding_config(result)
        return

    # Screen 5
    val = _run_select("Which safety profile should OpenShard use?", SAFETY_PROFILE_CHOICES)
    if val is not None:
        result["safety_profile"] = val
    if result["skipped"]:
        _write_onboarding_config(result)
        return

    # Screen 6 — local-first notice (also the "Help improve OpenShard" notice:
    # seeing it and continuing turns an undecided consent on; skipping does not)
    if _run_info(LOCAL_FIRST_NOTICE, footer="Enter finish setup   Esc skip"):
        _write_onboarding_config(result)
        return
    _record_telemetry_notice_seen()

    # Finish screen
    _show_finish_screen(result, _InfoScreen)
    _write_onboarding_config(result)


def _finish_summary_body(result: dict[str, Any]) -> str:
    """Build the human-readable finish summary from confirmed selections."""
    _label_map: dict[str, dict[str, str]] = {
        "user_type": {"human": "I'm a Human", "agent": "I'm an AI Agent", "demo": "Just exploring / demo"},
        "executor": {
            "native": "OpenShard Native",
            "cli_agent": "Connect an installed CLI agent",
            "review": "Review output from another tool",
            "agent_ci": "Agent / CI setup",
            "demo": "Demo mode",
            # Legacy values from older configs still render safely.
            **LEGACY_EXECUTOR_LABELS,
        },
        "provider_route": {"openrouter": "OpenRouter aggregator", "direct": "Direct provider API", "demo": "Demo / skip"},
        "provider": {"openrouter": "OpenRouter", "anthropic": "Anthropic", "openai": "OpenAI",
                     "google": "Google Gemini", "xai": "xAI Grok", "deepseek": "DeepSeek",
                     "moonshot": "Moonshot / Kimi", "glm": "GLM / Zhipu", "minimax": "MiniMax",
                     "skip": "None (demo mode)", "other": "Other"},
        "safety_profile": {"recommended": "Recommended", "strict": "Strict", "fast": "Fast"},
    }

    def _label(field: str) -> str:
        val = result.get(field)
        if val is None:
            return "-"
        return _label_map.get(field, {}).get(val, val)

    return (
        "Setup complete.\n\n"
        f"  Mode:     {_label('user_type')}\n"
        f"  Executor: {_label('executor')}\n"
        f"  Route:    {_label('provider_route')}\n"
        f"  Provider: {_label('provider')}\n"
        f"  Safety:   {_label('safety_profile')}\n"
        f"  Receipts:  Local only\n"
        f"  Telemetry: {telemetry_summary_line()}\n\n"
        f"Next commands:\n{NEXT_COMMANDS}\n\n"
        "Run `openshard doctor` to review your config at any time."
    )


def _show_finish_screen(result: dict[str, Any], _InfoScreen) -> None:
    app = _InfoScreen(_finish_summary_body(result), footer="Enter to continue")
    app.run()
