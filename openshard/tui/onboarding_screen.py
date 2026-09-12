"""TUI onboarding screen — pushed onto the screen stack on first run.

Shares choice data from openshard.onboarding.choices (neutral shared module).
Does not import from openshard.cli.ui.onboarding to avoid a CLI→TUI dependency.

Uses OptionList for arrow-key selection: ↑↓ navigate (OptionHighlighted),
Enter confirm (OptionSelected), Esc skip the whole flow.
"""
from __future__ import annotations

import datetime
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import OptionList, Static

from openshard.onboarding.choices import (
    DIRECT_PROVIDER_CHOICES,
    EXECUTOR_CHOICES,
    LEGACY_EXECUTOR_LABELS,
    LOCAL_FIRST_NOTICE,
    NEXT_COMMANDS,
    PROVIDER_ROUTE_CHOICES,
    SAFETY_PROFILE_CHOICES,
    USER_TYPE_CHOICES,
)

_SELECT_SCREENS = [
    ("user_type", "Who is using OpenShard?", USER_TYPE_CHOICES),
    ("executor", "How do you want to use OpenShard?", EXECUTOR_CHOICES),
    ("provider_route", "How do you want OpenShard to access models?", PROVIDER_ROUTE_CHOICES),
    # provider_direct is injected conditionally after provider_route = "direct"
    ("safety_profile", "Which safety profile should OpenShard use?", SAFETY_PROFILE_CHOICES),
]


def _marker_prompt(label: str, highlighted: bool) -> str:
    return f"> {label}" if highlighted else f"  {label}"


class OnboardingScreen(Screen):
    """Full-screen interactive onboarding pushed onto the TUI app on first run."""

    BINDINGS = [
        Binding("enter", "info_continue", "Continue", show=False),
        Binding("escape", "skip_all", "Skip setup", show=True),
        Binding("ctrl+q", "skip_all", "Skip setup", show=False),
    ]

    CSS = """
    OnboardingScreen {
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
    #body {
        color: #CCCCCC;
        margin: 1 2;
    }
    #footer {
        color: #555555;
        margin: 1 2;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._result: dict[str, Any] = {"skipped": False}
        self._screen_list = list(_SELECT_SCREENS)
        self._screen_idx = 0
        self._info_mode = False

    # ------------------------------------------------------------------
    # Compose / mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="question")
        yield OptionList(id="choice-set")
        yield Static("", id="note")
        yield Static("", id="body")
        yield Static("", id="footer")

    def on_mount(self) -> None:
        self._show_select_screen()

    # ------------------------------------------------------------------
    # Select-screen rendering
    # ------------------------------------------------------------------

    def _current_choices(self) -> list[tuple[str, str, str, bool]]:
        return self._screen_list[self._screen_idx][2]

    def _show_select_screen(self) -> None:
        self._info_mode = False
        field, question, choices = self._screen_list[self._screen_idx]

        self.query_one("#question", Static).update(question)
        self.query_one("#body", Static).update("")
        self.query_one("#note", Static).update("")

        ol = self.query_one("#choice-set", OptionList)
        ol.clear_options()
        ol.add_options([_marker_prompt(label, i == 0) for i, (label, *_r) in enumerate(choices)])
        ol.display = True
        if choices:
            ol.highlighted = 0
            self._refresh_note(0)
        ol.focus()

        self.query_one("#footer", Static).update(
            "↑↓ navigate   Enter confirm   Esc skip setup"
        )

    def _refresh_markers(self, highlighted: int) -> None:
        ol = self.query_one("#choice-set", OptionList)
        for i, (label, *_r) in enumerate(self._current_choices()):
            ol.replace_option_prompt_at_index(i, _marker_prompt(label, i == highlighted))

    def _refresh_note(self, index: int) -> None:
        _label, _value, note, _planned = self._current_choices()[index]
        self.query_one("#note", Static).update(f"↳ {note}" if note else "")

    # ------------------------------------------------------------------
    # Info-screen rendering
    # ------------------------------------------------------------------

    def _show_info(self, body: str, footer: str) -> None:
        self._info_mode = True
        self.query_one("#question", Static).update("")
        self.query_one("#note", Static).update("")
        ol = self.query_one("#choice-set", OptionList)
        ol.display = False
        self.query_one("#body", Static).update(body)
        self.query_one("#footer", Static).update(footer)
        self.set_focus(None)  # so the screen-level Enter binding fires

    def _show_key_status(self) -> None:
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
        self._show_info("\n".join(lines), footer="Enter continue   Esc skip setup")

    def _show_local_first(self) -> None:
        self._show_info(LOCAL_FIRST_NOTICE, footer="Enter finish setup   Esc skip")

    def _show_finish(self) -> None:
        self._show_info(self._finish_body(), footer="Enter to continue")

    def _finish_body(self) -> str:
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

        def _l(field: str) -> str:
            val = self._result.get(field)
            if val is None:
                return "-"
            return _label_map.get(field, {}).get(val, val)

        return (
            "Setup complete.\n\n"
            f"  Mode:     {_l('user_type')}\n"
            f"  Executor: {_l('executor')}\n"
            f"  Route:    {_l('provider_route')}\n"
            f"  Provider: {_l('provider')}\n"
            f"  Safety:   {_l('safety_profile')}\n"
            f"  Data:     Local-first (privacy-safe telemetry: openshard telemetry status)\n\n"
            f"Next commands:\n{NEXT_COMMANDS}\n\n"
            "Run `openshard doctor` to review your config at any time."
        )

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @on(OptionList.OptionHighlighted, "#choice-set")
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._info_mode:
            return
        self._refresh_markers(event.option_index)
        self._refresh_note(event.option_index)

    @on(OptionList.OptionSelected, "#choice-set")
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        if self._info_mode:
            return
        field, _question, choices = self._screen_list[self._screen_idx]
        _label, value, _note, _planned = choices[event.option_index]
        self._result[field] = value

        if field == "provider_route":
            if value == "openrouter":
                self._result["provider"] = "openrouter"
            elif value == "demo":
                self._result["provider"] = "skip"
            elif value == "direct":
                insert_at = self._screen_idx + 1
                if insert_at >= len(self._screen_list) or self._screen_list[insert_at][0] != "provider":
                    self._screen_list.insert(
                        insert_at, ("provider", "Choose direct provider:", DIRECT_PROVIDER_CHOICES)
                    )

        self._advance()

    def action_info_continue(self) -> None:
        if self._info_mode:
            self._advance()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _advance(self) -> None:
        self._screen_idx += 1
        n_select = len(self._screen_list)

        if self._screen_idx < n_select:
            self._show_select_screen()
        elif self._screen_idx == n_select:
            self._show_key_status()
        elif self._screen_idx == n_select + 1:
            self._show_local_first()
        elif self._screen_idx == n_select + 2:
            # The local-first / "Help improve OpenShard" notice was seen and
            # continued past: an undecided consent becomes on (skipping does not).
            self._record_telemetry_notice_seen()
            self._show_finish()
        else:
            self._finish()

    @staticmethod
    def _record_telemetry_notice_seen() -> None:
        try:
            from openshard.telemetry.state import consent_after_notice

            consent_after_notice(source="onboarding")
        except Exception:
            pass

    def _finish(self) -> None:
        self._write_config()
        self.dismiss()

    def action_skip_all(self) -> None:
        self._result["skipped"] = True
        self._write_config()
        self.dismiss()

    # ------------------------------------------------------------------
    # Config write
    # ------------------------------------------------------------------

    _INJECTED_KEY_FIELDS = ("openrouter_api_key", "anthropic_api_key", "openai_api_key")

    def _write_config(self) -> None:
        from openshard.config.settings import load_config_safe, save_config

        base, _valid, _path = load_config_safe()
        for _k in self._INJECTED_KEY_FIELDS:
            base.pop(_k, None)
        onboarding_block: dict[str, Any] = {
            "schema_version": 1,
            "completed_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "skipped": self._result.get("skipped", False),
        }
        for field in ("user_type", "executor", "provider_route", "provider", "safety_profile"):
            if self._result.get(field) is not None:
                onboarding_block[field] = self._result[field]
        base["onboarding"] = onboarding_block
        save_config(base)
