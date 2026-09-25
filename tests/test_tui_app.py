from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from textual.containers import ScrollableContainer
from textual.widgets import Label, Static

from openshard.tui.app import (
    OpenShardTui,
    TaskInput,
    _extract_receipt_block,
    _extract_run_display,
    _render_openshard_block,
    _render_user_block,
    _strip_run_timeline_block,
)
from openshard.tui.state import get_guardrails

_SIZE = (120, 55)


def _make_app(tmp_path, recent_runs=None, git_info=None):
    app = OpenShardTui(path=tmp_path)
    app._git_info = git_info or {"project_name": "test-project", "branch": "main", "state": "clean"}
    app._recent_runs = [] if recent_runs is None else recent_runs
    app._guardrails = get_guardrails()
    return app


def _text(widget) -> str:
    return str(widget.content)


# ── Compact header ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_header_brand_contains_openshard(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#header-brand", Static))
        assert "█" in text or "OpenShard" in text


@pytest.mark.asyncio
async def test_header_tagline_contains_expected_text(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        assert "layer for AI coding agents" in _text(app.query_one("#header-tagline", Static))


@pytest.mark.asyncio
async def test_header_version_is_displayed(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#header-version", Static))
        assert text.startswith("v")


@pytest.mark.asyncio
async def test_header_project_shows_project_name(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        assert "test-project" in _text(app.query_one("#header-project", Static))


@pytest.mark.asyncio
async def test_header_project_shows_branch(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        assert "main" in _text(app.query_one("#header-project", Static))


@pytest.mark.asyncio
async def test_header_project_shows_clean_state(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        assert "Clean" in _text(app.query_one("#header-project", Static))


@pytest.mark.asyncio
async def test_dirty_state_renders_as_changes_pending(tmp_path):
    app = _make_app(tmp_path, git_info={"project_name": "p", "branch": "main", "state": "dirty"})
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#header-project", Static))
        assert "Changes pending" in text
        assert "dirty" not in text


@pytest.mark.asyncio
async def test_header_project_row_contains_column_labels(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#header-project", Static))
        for label in ("PROJECT", "BRANCH", "REPO"):
            assert label in text


# ── Status strip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_strip_shows_expected_content(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#status-strip", Static))
        for term in ("Sandbox [OFF]", "Receipts [ON]", "Checks [AUTO]", "Approval [SMART]"):
            assert term in text


@pytest.mark.asyncio
async def test_no_forbidden_terms_in_status_strip(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#status-strip", Static)).lower()
        for forbidden in ("routing advisory", "verification loop", "plan ledger"):
            assert forbidden not in text, f"Found '{forbidden}' in #status-strip"
        assert "candidate" not in text, "Found 'candidate' in #status-strip"


@pytest.mark.asyncio
async def test_status_strip_shows_bracketed_values_not_bare_labels(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        text = _text(app.query_one("#status-strip", Static))
        # Brackets must be present — bare labels without values are not acceptable
        assert "[ON]" in text
        assert "[AUTO]" in text
        assert "[SMART]" in text


# ── Mode strip ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mode_strip_shows_mode_and_executor(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        left = _text(app.query_one("#mode-strip-left", Static))
        right = _text(app.query_one("#mode-strip-right", Static))
        assert "Auto mode" in left
        assert "OpenShard Native" in right


# ── Slash command menu ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_slash_menu_hidden_when_composer_empty(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        slash_menu = app.query_one("#slash-menu", Static)
        assert slash_menu.display is False


@pytest.mark.asyncio
async def test_slash_menu_shows_when_text_starts_with_slash(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.load_text("/")
        await pilot.pause()
        slash_menu = app.query_one("#slash-menu", Static)
        assert slash_menu.display is True


@pytest.mark.asyncio
async def test_slash_menu_hides_after_submit(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/help")
        await pilot.press("enter")
        await pilot.pause()
        slash_menu = app.query_one("#slash-menu", Static)
        assert slash_menu.display is False


@pytest.mark.asyncio
async def test_slash_menu_shows_for_partial_command(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.load_text("/p")
        await pilot.pause()
        assert app.query_one("#slash-menu", Static).display is True


@pytest.mark.asyncio
async def test_slash_menu_hidden_when_command_has_trailing_space(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.load_text("/pack ")
        await pilot.pause()
        assert app.query_one("#slash-menu", Static).display is False


@pytest.mark.asyncio
async def test_slash_menu_hidden_when_command_has_argument(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.load_text("/pack production-iac-hardening")
        await pilot.pause()
        assert app.query_one("#slash-menu", Static).display is False


# ── Task composer ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_input_is_multiline_composer(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        ta = app.query_one("#task-input", TaskInput)
        assert ta is not None
        assert ta.BORDER_TITLE == "Type a task or / for commands"


@pytest.mark.asyncio
async def test_empty_input_does_not_set_status(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        app.query_one("#task-input", TaskInput).focus()
        await pilot.press("enter")
        assert _text(app.query_one("#status-msg", Label)).strip() == ""


@pytest.mark.asyncio
async def test_enter_clears_input(tmp_path):
    app = _make_app(tmp_path)
    # This test only verifies composer clearing. Stub the background CLI worker
    # so it cannot outlive the test app and race with screen teardown on slower
    # Windows runners.
    with patch.object(app, "_run_cli_async") as mock_run:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("some task")
            await pilot.press("enter")
            assert ta.text == ""
        mock_run.assert_called_once()


# ── Output panel ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_panel_is_present(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as _:
        panel = app.query_one("#output-panel", ScrollableContainer)
        assert panel is not None


@pytest.mark.asyncio
async def test_help_command_shows_help_in_panel(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/help")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Supported commands" in text
        assert "/last" in text
        assert "/last full" in text
        assert "/quit" in text


@pytest.mark.asyncio
async def test_unknown_slash_command_shows_error(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/badcommand")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Unknown command" in text


@pytest.mark.asyncio
async def test_clear_command_empties_panel(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        # First populate the panel via /help
        ta.load_text("/help")
        await pilot.press("enter")
        # Then clear it
        ta.load_text("/clear")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert text.strip() == ""


@pytest.mark.asyncio
async def test_quit_command_exits_app(tmp_path):
    app = _make_app(tmp_path)
    with patch.object(app, "exit") as mock_exit:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/quit")
            await pilot.press("enter")
        mock_exit.assert_called_once()


@pytest.mark.asyncio
async def test_plain_task_invokes_cli_runner(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "some output"
    mock_result.exit_code = 0
    mock_result.exception = None

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("explain this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)

        calls = mock_runner_cls.return_value.invoke.call_args_list
        assert len(calls) == 1
        # CliRunner.invoke(cli, ["run", task], input="", catch_exceptions=True)
        assert "run" in str(calls[0])
        assert "explain this repo" in str(calls[0])


@pytest.mark.asyncio
async def test_run_output_appears_in_panel(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Result: all good\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("explain this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
        assert "Result: all good" in text
        assert "Done." in text


@pytest.mark.asyncio
async def test_failed_run_shows_failure_status(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Error occurred\n"
    mock_result.exit_code = 1
    mock_result.exception = None

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("break something")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
        assert "Failed" in text


@pytest.mark.asyncio
async def test_failed_run_shows_diagnostic_tail(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    # Error buried beyond the last-30-line fallback of _extract_run_display
    lines = [f"  line {i}" for i in range(40)]
    lines.append("Error: Authentication failed. Check that your provider API key is valid.")
    mock_result.output = "\n".join(lines) + "\n"
    mock_result.exit_code = 1
    mock_result.exception = None

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("do something that fails")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "Failed (exit 1)" in text
    assert "[argv]" in text
    assert "[exit] 1" in text
    assert "Authentication failed" in text


@pytest.mark.asyncio
async def test_failed_run_diagnostic_does_not_expose_structured_findings_in_argv(tmp_path):
    # Simulate the pack flow: suffix is stored in _pack_suffix, NOT typed by the user.
    # The input field holds only the visible prompt; the hidden suffix is appended
    # programmatically so it must never appear in the diagnostic argv preview.
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Error: something went wrong\n"
    mock_result.exit_code = 1
    mock_result.exception = None

    hidden_suffix = (
        "\n\nAfter completing your analysis, put the following content "
        'in the JSON `summary` field:\nSTRUCTURED_FINDINGS: [{"severity": "Critical"}]'
    )

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            # Inject pack state the same way PACK_SHOW would set it
            app._pack_suffix = hidden_suffix
            app._pack_workflow = "native"
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("Review this Terraform config")  # visible prompt only
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "[argv]" in text
    assert "STRUCTURED_FINDINGS" not in text
    assert "After completing your analysis" not in text


@pytest.mark.asyncio
async def test_failed_run_diagnostic_does_not_expose_structured_findings_in_output(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    # output itself leaks STRUCTURED_FINDINGS (e.g. task text echoed in a Click error)
    mock_result.output = (
        "Running task: review stuff\n"
        'STRUCTURED_FINDINGS: [{"severity": "High", "message": "leaked secret"}]\n'
        "Error: something went wrong\n"
    )
    mock_result.exit_code = 1
    mock_result.exception = None

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("plain task text")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "[exit] 1" in text
    assert "STRUCTURED_FINDINGS" not in text
    assert "leaked secret" not in text


@pytest.mark.asyncio
async def test_recent_activity_refreshes_after_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "done\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    new_runs = [{"task": "refreshed task", "timestamp": "2026-05-18T00:00", "status": "passed", "duration": "2.0s"}]

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        with patch("openshard.tui.app.OpenShardTui._on_cli_result", wraps=app._on_cli_result):
            with patch("openshard.tui.state.load_recent_runs", return_value=new_runs) as mock_load:
                async with app.run_test(size=_SIZE) as pilot:
                    ta = app.query_one("#task-input", TaskInput)
                    ta.focus()
                    ta.load_text("explain this repo")
                    await pilot.press("enter")
                    await pilot.pause(delay=0.3)
                mock_load.assert_called()


# ── Workflow packs commands ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_packs_command_shows_workflow_packs_heading(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Workflow packs" in text


@pytest.mark.asyncio
async def test_packs_command_shows_known_pack_ids(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "production-iac-hardening" in text
        assert "repo-explanation" in text


@pytest.mark.asyncio
async def test_packs_command_shows_usage_hint(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "/pack <pack-id>" in text


@pytest.mark.asyncio
async def test_pack_show_renders_title(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Production IaC hardening review" in text


@pytest.mark.asyncio
async def test_pack_show_does_not_render_summary(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Hardens" not in text


@pytest.mark.asyncio
async def test_pack_show_renders_compact_selected_message(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Workflow pack selected:" in text


@pytest.mark.asyncio
async def test_pack_prompt_text_not_in_output_panel(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Terraform" not in text


@pytest.mark.asyncio
async def test_pack_show_unknown_id_shows_error(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack unknown-xyz")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Unknown pack" in text
        assert "repo-explanation" in text


@pytest.mark.asyncio
async def test_pack_no_id_shows_usage(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Usage: /pack" in text


@pytest.mark.asyncio
async def test_packs_command_does_not_invoke_cli_runner(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/packs")
            await pilot.press("enter")
        mock_runner_cls.return_value.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_pack_show_does_not_invoke_cli_runner(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/pack production-iac-hardening")
            await pilot.press("enter")
        mock_runner_cls.return_value.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_packs_output_no_forbidden_strings(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        for forbidden in ("Tunic Pay", "Mercury", "Volant", "AKIA"):
            assert forbidden not in text


@pytest.mark.asyncio
async def test_pack_show_preloads_prompt_into_composer(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack production-iac-hardening")
        await pilot.press("enter")
        # Pack prompt should be preloaded into the composer
        assert "Terraform" in ta.text


@pytest.mark.asyncio
async def test_packs_with_id_shows_pack_selected_message(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Workflow pack selected:" in text
        assert "Unknown command" not in text


@pytest.mark.asyncio
async def test_packs_with_id_loads_prompt_into_composer(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs production-iac-hardening")
        await pilot.press("enter")
        assert "Terraform" in ta.text


@pytest.mark.asyncio
async def test_packs_with_id_does_not_print_full_prompt_in_panel(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/packs production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
        assert "Terraform" not in text


# ── _extract_receipt_block() unit tests ───────────────────────────────────


def test_extract_receipt_separator_format():
    output = (
        "Planning - mapping out implementation approach...\n"
        "Executing - Sonnet 4.6 handling logic...\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RECEIPT — shard-20260520-0001\n"
        "1 file changed. Verification passed. Receipt saved.\n"
    )
    result = _extract_receipt_block(output)
    assert result is not None
    assert "RECEIPT" in result
    assert "1 file changed" in result
    # separator line is included
    assert "━" in result
    # noisy lines are excluded
    assert "Planning" not in result


def test_extract_receipt_rich_box_format():
    output = (
        "Planning - mapping out...\n"
        "Executing - step 1...\n"
        "╭─ OpenShard Receipt ────────────────────────────╮\n"
        "│ 2 files changed. Verification passed.          │\n"
        "╰────────────────────────────────────────────────╯\n"
    )
    result = _extract_receipt_block(output)
    assert result is not None
    assert "╭─ OpenShard Receipt" in result
    assert "2 files changed" in result
    assert "Planning" not in result


def test_extract_receipt_neither_returns_none():
    output = (
        "Planning - mapping out implementation approach...\n"
        "Executing - Sonnet 4.6 handling logic...\n"
        "Some other output line.\n"
    )
    assert _extract_receipt_block(output) is None


def test_extract_receipt_separator_preferred_over_rich_box():
    output = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RECEIPT — shard-20260520-0002\n"
        "Receipt saved.\n"
        "╭─ OpenShard Receipt ──╮\n"
        "│ also here            │\n"
        "╰──────────────────────╯\n"
    )
    result = _extract_receipt_block(output)
    assert result is not None
    assert result.startswith("━")


# ── _extract_run_display() unit tests ─────────────────────────────────────


def test_extract_run_display_preserves_review_complete_and_receipt():
    output = (
        "Bootstrap noise...\n"
        "\n"
        "Review complete\n"
        "Found 3 issues worth addressing.\n"
        "\n"
        "Critical\n"
        "✖  Something bad\n"
        "\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RECEIPT — shard-20260520-0001\n"
        "3 issues found; review files created.\n"
    )
    result = _extract_run_display(output)
    assert "Review complete" in result
    assert "Found 3 issues" in result
    assert "Critical" in result
    assert "RECEIPT" in result
    assert "Bootstrap noise" not in result


def test_extract_run_display_preserves_openshard_completed_fallback():
    # No-files fallback: "no structured findings were captured" (not "generated review files")
    output = (
        "Noise before\n"
        "OpenShard completed the review, but no structured findings were captured for this run.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RECEIPT — shard-20260520-0002\n"
        "Review run. Receipt saved.\n"
    )
    result = _extract_run_display(output)
    assert "OpenShard completed the review" in result
    assert "RECEIPT" in result
    assert "Noise before" not in result


def test_extract_run_display_no_memo_falls_back_to_receipt():
    output = (
        "Planning - step 1...\n"
        "Executing - step 2...\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RECEIPT — shard-20260520-0003\n"
        "1 file changed. Receipt saved.\n"
    )
    result = _extract_run_display(output)
    assert "RECEIPT" in result
    assert "1 file changed" in result
    assert "Planning" not in result


def test_extract_run_display_no_receipt_no_memo_returns_tail():
    output = "\n".join(f"line {i}" for i in range(50))
    result = _extract_run_display(output)
    assert "line 49" in result
    assert "line 0" not in result


@pytest.mark.asyncio
async def test_run_output_shows_activity_feed_checkmarks(tmp_path):
    """Activity feed checkmarks appear in the TUI output panel after a run."""
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = (
        "  . Running...\n"
        "Review complete\n"
        "\n"
        "Running production IaC review\n"
        "\n"
        "  ✓ Loaded workflow pack\n"
        "  ✓ Scanned repo\n"
        "  ✓ Model responded\n"
        "  ✓ Saved Shard receipt\n"
        "\n"
        "━" * 40 + "\n"
        "  RECEIPT — shard-20260523-0001\n"
        "  Task   review stuff\n"
        "━" * 40 + "\n"
    )
    mock_result.exit_code = 0
    mock_result.exception = None

    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review stuff")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "✓" in text or "+" in text, "Activity feed checkmarks not found in output panel"
    assert "chain of thought" not in text.lower()
    assert "reasoning trace" not in text.lower()
    assert "STRUCTURED_FINDINGS" not in text


def test_extract_run_display_does_not_contain_structured_findings_json():
    output = (
        'STRUCTURED_FINDINGS: [{"severity": "Critical", "message": "Bad thing"}]\n'
        "Review complete\n"
        "Found 1 issue worth addressing.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "RECEIPT — shard-20260520-0004\n"
        "1 issue found.\n"
    )
    result = _extract_run_display(output)
    # The extraction starts at "Review complete", which is after the STRUCTURED_FINDINGS line
    assert "STRUCTURED_FINDINGS" not in result


# ── Feedback TUI dispatch (parse-level) ───────────────────────────────────


def test_feedback_parse_produces_expected_cli_args():
    """Parse → build the CLI args the app would pass to _run_cli_async."""
    from openshard.tui.commands import TuiCommand, parse_tui_input
    parsed = parse_tui_input("/feedback accepted")
    assert parsed.cmd == TuiCommand.FEEDBACK
    args = ["feedback", "--outcome", parsed.feedback_outcome]
    assert args == ["feedback", "--outcome", "accepted"]


def test_feedback_with_reason_produces_expected_cli_args():
    from openshard.tui.commands import TuiCommand, parse_tui_input
    parsed = parse_tui_input("/feedback rejected wording wrong")
    assert parsed.cmd == TuiCommand.FEEDBACK
    args = ["feedback", "--outcome", parsed.feedback_outcome]
    if parsed.feedback_reason:
        args += ["--reason", parsed.feedback_reason]
    assert args == ["feedback", "--outcome", "rejected", "--reason", "wording wrong"]


# ── Transcript separation labels (v1) ─────────────────────────────────────


def _cli_ok(output: str = "ok\n") -> MagicMock:
    r = MagicMock()
    r.output = output
    r.exit_code = 0
    r.exception = None
    return r


@pytest.mark.asyncio
async def test_transcript_you_and_openshard_for_plain_task(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok()
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("explain this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text
    assert "explain this repo" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_transcript_you_and_openshard_for_pack(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/pack production-iac-hardening")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text
    assert "OPENSHARD" in text
    assert "Workflow pack selected:" in text


@pytest.mark.asyncio
async def test_transcript_you_and_openshard_for_last(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok("last output\n")
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/last")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_transcript_you_and_openshard_for_last_more(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok("last more output\n")
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/last more")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_transcript_you_and_openshard_for_last_full(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok("last full output\n")
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/last full")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_transcript_you_and_openshard_for_feedback(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok("Feedback recorded: partial\n")
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/feedback partial")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_transcript_clear_still_empties_panel(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/help")
        await pilot.press("enter")
        await pilot.pause()
        ta.load_text("/clear")
        await pilot.press("enter")
        await pilot.pause()
        text = _text(app.query_one("#output-content", Static))
    assert text.strip() == ""


# ── _render_user_block / _render_openshard_block unit tests ───────────────


def test_render_user_block_contains_you_label():
    result = _render_user_block("/last more")
    assert "YOU" in result


def test_render_user_block_contains_command_text():
    result = _render_user_block("/last more")
    assert "/last more" in result


def test_render_user_block_contains_guide_rail():
    result = _render_user_block("/last more")
    assert "│" in result


def test_render_user_block_multiline_preserves_all_lines():
    result = _render_user_block("line one\nline two")
    assert "line one" in result
    assert "line two" in result


def test_render_openshard_block_contains_label():
    result = _render_openshard_block("some output\nDone.")
    assert "OPENSHARD" in result


def test_render_openshard_block_body_unchanged():
    body = "some output\nDone."
    result = _render_openshard_block(body)
    assert "some output" in result
    assert "Done." in result


def test_render_openshard_block_does_not_indent_body():
    result = _render_openshard_block("RECEIPT — shard-001\n1 file changed.")
    lines = result.splitlines()
    body_lines = [ln for ln in lines if "RECEIPT" in ln or "1 file" in ln]
    for line in body_lines:
        assert not line.startswith(" "), f"Body line was unexpectedly indented: {line!r}"


# ---------------------------------------------------------------------------
# Action block integration tests
# ---------------------------------------------------------------------------

_FAKE_ENTRY_WITH_TIMELINE = {
    "run_timeline": [
        {"label": "Loaded workflow pack", "status": "completed"},
        {"label": "Scanned repo", "status": "completed"},
        {"label": "Found raw findings", "status": "completed", "count": 5},
        {"label": "Saved Shard receipt", "status": "completed"},
    ],
    "review_checks": [],
}

_FAKE_ENTRY_WITH_CHECKS = {
    "run_timeline": [
        {"label": "Scanned repo", "status": "completed"},
    ],
    "review_checks": [
        {"name": "terraform fmt", "status": "skipped", "reason": "terraform not installed"},
        {"name": "tflint", "status": "skipped", "reason": "tflint not installed"},
    ],
}

_FAKE_ENTRY_EMPTY = {
    "run_timeline": [],
    "review_checks": [],
}

_FAKE_ENTRY_WITH_EVIDENCE = {
    "run_timeline": [{"label": "Scanned repo", "status": "completed"}],
    "review_checks": [],
    "file_context": {"paths": ["src/main.py", "demo-task.md"]},
    "findings": [{"severity": "High", "message": "Bad config", "path": "database.tf"}],
}

_FAKE_ENTRY_MULTI_ROLE = {
    "run_timeline": [{"label": "Scanned repo", "status": "completed"}],
    "review_checks": [],
    "file_context": {"paths": ["shared.tf"]},
    "findings": [{"severity": "High", "message": "Open port", "path": "shared.tf"}],
}

_FAKE_ENTRY_NO_EVIDENCE = {
    "run_timeline": [{"label": "Scanned repo", "status": "completed"}],
    "review_checks": [],
    "file_context": {"paths": []},
    "findings": [],
}


@pytest.mark.asyncio
async def test_action_blocks_appear_after_successful_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Result: all good\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_TIMELINE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "ACTIONS" in text
    assert "Loaded workflow pack" in text
    assert "Scanned repo" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_check_actions_appear_after_run_with_checks(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Result: all good\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_CHECKS),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "CHECK ACTIONS" in text
    assert "terraform fmt" in text
    assert "tflint" in text


@pytest.mark.asyncio
async def test_action_blocks_absent_when_no_timeline(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Result: all good\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_EMPTY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "ACTIONS" not in text
    assert "CHECK ACTIONS" not in text


@pytest.mark.asyncio
async def test_action_blocks_absent_when_entry_missing(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Result: all good\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=None),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "ACTIONS" not in text


@pytest.mark.asyncio
async def test_original_output_preserved_after_action_blocks(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260523-0001\nAll checks passed.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_TIMELINE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "ACTIONS" in text
    assert "Done." in text


@pytest.mark.asyncio
async def test_action_blocks_absent_on_failed_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Error: something went wrong\n"
    mock_result.exit_code = 1
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_TIMELINE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "ACTIONS" not in text
    assert "Failed" in text


@pytest.mark.asyncio
async def test_last_more_output_has_no_action_blocks(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260523-0001\nFull details here.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_TIMELINE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/last more")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "ACTIONS" not in text


@pytest.mark.asyncio
async def test_clear_still_empties_output_after_action_blocks(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Result: all good\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_TIMELINE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            # now clear
            ta.load_text("/clear")
            await pilot.press("enter")
            await pilot.pause(delay=0.1)
            text = _text(app.query_one("#output-content", Static))

    assert text.strip() == ""


# ---------------------------------------------------------------------------
# Evidence block integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_block_appears_after_successful_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260524-0001\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_EVIDENCE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "EVIDENCE" in text
    assert "Read src/main.py" in text
    assert "Finding source database.tf" in text


@pytest.mark.asyncio
async def test_evidence_absent_when_no_file_evidence(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260524-0001\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_NO_EVIDENCE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "EVIDENCE" not in text


@pytest.mark.asyncio
async def test_failed_run_does_not_show_evidence(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Error: something went wrong\n"
    mock_result.exit_code = 1
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_EVIDENCE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "EVIDENCE" not in text
    assert "Failed" in text


@pytest.mark.asyncio
async def test_last_more_does_not_show_evidence(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260524-0001\nFull details here.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_EVIDENCE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/last more")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "EVIDENCE" not in text


@pytest.mark.asyncio
async def test_evidence_ordering_after_actions(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260524-0001\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_EVIDENCE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert text.index("ACTIONS") < text.index("EVIDENCE")


@pytest.mark.asyncio
async def test_evidence_multi_role_file_renders_once(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260524-0001\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_MULTI_ROLE),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert text.count("shared.tf") == 1


# ---------------------------------------------------------------------------
# _strip_run_timeline_block unit tests
# ---------------------------------------------------------------------------

_TIMELINE_BLOCK = (
    "\nRunning Production IaC hardening review\n"
    "\n"
    "  ✓ Loaded workflow pack\n"
    "  ✓ Scanned repo\n"
    "  ✓ Routed to Sonnet 4.6\n"
    "  ✓ Saved Shard receipt\n"
)

_TIMELINE_BLOCK_ASCII = (
    "\nRunning my task\n"
    "\n"
    "  + Loaded workflow pack\n"
    "  - tflint\n"
)


def test_strip_run_timeline_block_removes_block():
    text = "Review complete\nFound 5 issues." + _TIMELINE_BLOCK + "\nRECEIPT — shard-001"
    result = _strip_run_timeline_block(text)
    assert "Running Production IaC hardening review" not in result
    assert "Loaded workflow pack" not in result
    assert "Review complete" in result
    assert "RECEIPT — shard-001" in result


def test_strip_run_timeline_block_ascii_symbols():
    text = "Review complete\n" + _TIMELINE_BLOCK_ASCII + "\nRECEIPT — shard-001"
    result = _strip_run_timeline_block(text)
    assert "Running my task" not in result
    assert "RECEIPT — shard-001" in result


def test_strip_run_timeline_block_no_false_positive():
    # "Running" as part of a sentence, not a standalone header
    text = "Review complete\nRunning total: 5\n  indented but no symbol pattern"
    result = _strip_run_timeline_block(text)
    assert "Running total: 5" in result


def test_strip_run_timeline_block_passthrough_when_no_block():
    text = "Review complete\nFound 5 issues.\n\nRECEIPT — shard-001"
    result = _strip_run_timeline_block(text)
    assert result == text


def test_strip_run_timeline_block_preserves_content_before_and_after():
    before = "Review complete\nSome memo content."
    after = "\nRECEIPT — shard-001\nAll good."
    text = before + _TIMELINE_BLOCK + after
    result = _strip_run_timeline_block(text)
    assert "Review complete" in result
    assert "Some memo content." in result
    assert "RECEIPT — shard-001" in result
    assert "All good." in result


def test_strip_run_timeline_block_cleans_preceding_blank():
    # The blank line before "Running …" should also be cleaned up
    text = "Some content.\n\nRunning my run\n\n  ✓ Step one\n  ✓ Step two\n\nNext section"
    result = _strip_run_timeline_block(text)
    assert "Running my run" not in result
    # Should not have double-blank where the block was
    assert "\n\n\n" not in result


# ---------------------------------------------------------------------------
# Integration: duplicate timeline stripped when action_summary present
# ---------------------------------------------------------------------------

_FAKE_ENTRY_FOR_STRIP = {
    "run_timeline": [
        {"label": "Loaded workflow pack", "status": "completed"},
        {"label": "Scanned repo", "status": "completed"},
    ],
    "review_checks": [],
}

_RUN_OUTPUT_WITH_TIMELINE = (
    "\nReview complete\n"
    "Found 5 issues worth addressing.\n"
    "\nRunning Production IaC hardening review\n"
    "\n"
    "  ✓ Loaded workflow pack\n"
    "  ✓ Scanned repo\n"
    "\n"
    "RECEIPT — shard-20260523-0001\n"
    "result: 5 issues\n"
)


@pytest.mark.asyncio
async def test_duplicate_timeline_block_stripped_when_action_summary_present(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = _RUN_OUTPUT_WITH_TIMELINE
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_FOR_STRIP),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    # Structured ACTIONS block still present
    assert "ACTIONS" in text
    # Duplicate "Running Production IaC hardening review" checklist is gone
    assert "Running Production IaC hardening review" not in text
    # Original result content preserved
    assert "Review complete" in text
    assert "RECEIPT" in text


@pytest.mark.asyncio
async def test_spacing_blank_line_between_openshard_and_actions(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Review complete\nAll good.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_FOR_STRIP),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    # The display string has a leading "\n" before ACTIONS, so in the raw content
    # the OPENSHARD label and ACTIONS are separated by at least one blank line
    assert "OPENSHARD" in text
    assert "ACTIONS" in text
    openshard_pos = text.find("OPENSHARD")
    actions_pos = text.find("ACTIONS")
    between = text[openshard_pos:actions_pos]
    assert between.count("\n") >= 2


# ---------------------------------------------------------------------------
# RESULT block integration tests
# ---------------------------------------------------------------------------

_FAKE_ENTRY_WITH_SUMMARY = {
    "run_timeline": [
        {"label": "Scanned repo", "status": "completed"},
    ],
    "review_checks": [],
    "summary": "All security checks passed with no critical findings.",
    "form_factor": {"risk_level": "low"},
    "write_path": "pipeline",
    "estimated_cost": 0.048,
}


@pytest.mark.asyncio
async def test_result_block_appears_on_fresh_successful_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Review complete\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_SUMMARY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "RESULT" in text
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_result_block_appears_before_actions_on_fresh_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Review complete\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_SUMMARY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "RESULT" in text
    assert "ACTIONS" in text
    assert text.find("RESULT") < text.find("ACTIONS")


@pytest.mark.asyncio
async def test_result_block_not_shown_on_failed_run(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Error: something went wrong\n"
    mock_result.exit_code = 1
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_SUMMARY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "RESULT" not in text
    assert "Failed" in text


@pytest.mark.asyncio
async def test_result_block_not_shown_when_no_entry(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Review complete\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=None),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "RESULT" not in text


@pytest.mark.asyncio
async def test_result_block_shows_cost_when_present(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Review complete\nDone.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_SUMMARY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "Cost" in text
    assert "$0.0480" in text


@pytest.mark.asyncio
async def test_original_output_preserved_alongside_result_block(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "Review complete\nAll checks passed.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_SUMMARY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("review this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "RESULT" in text
    assert "All checks passed." in text


@pytest.mark.asyncio
async def test_last_more_output_has_no_result_block(tmp_path):
    app = _make_app(tmp_path)
    mock_result = MagicMock()
    mock_result.output = "RECEIPT — shard-20260524-0001\nFull details here.\n"
    mock_result.exit_code = 0
    mock_result.exception = None

    with (
        patch("openshard.tui.app.CliRunner") as mock_runner_cls,
        patch("openshard.tui.state.load_last_run_entry", return_value=_FAKE_ENTRY_WITH_SUMMARY),
    ):
        mock_runner_cls.return_value.invoke.return_value = mock_result
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/last more")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
            text = _text(app.query_one("#output-content", Static))

    assert "RESULT" not in text


# ── Ask mode dispatch ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ask_renders_openshard_block(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/ask what models do you support?")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_ask_renders_you_block(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/ask what models")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text


@pytest.mark.asyncio
async def test_ask_does_not_call_cli_runner(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/ask what models do you have")
            await pilot.press("enter")
        mock_runner_cls.return_value.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_ask_does_not_start_run_status(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        with patch.object(app, "_start_run_status") as mock_start_run:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/ask what models do you have")
            await pilot.press("enter")
            mock_start_run.assert_not_called()


@pytest.mark.asyncio
async def test_fast_path_what_models_renders_openshard_block(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("what models do you have")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_fast_path_does_not_invoke_runner(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("what models do you have")
            await pilot.press("enter")
        mock_runner_cls.return_value.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_regression_normal_task_still_invokes_runner(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok()
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("explain this repo")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
    assert mock_runner_cls.return_value.invoke.called
    assert "run" in str(mock_runner_cls.return_value.invoke.call_args_list[0])


# ── Plan mode dispatch ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_renders_openshard_block(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/plan refactor auth safely")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_plan_renders_you_block(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/plan refactor auth safely")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "YOU" in text


@pytest.mark.asyncio
async def test_plan_output_contains_plan_section(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/plan refactor auth safely")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "PLAN" in text


@pytest.mark.asyncio
async def test_plan_does_not_invoke_cli_runner(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/plan refactor auth safely")
            await pilot.press("enter")
        mock_runner_cls.return_value.invoke.assert_not_called()


@pytest.mark.asyncio
async def test_plan_does_not_start_run_status(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        with patch.object(app, "_start_run_status") as mock_start_run:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("/plan refactor auth safely")
            await pilot.press("enter")
            mock_start_run.assert_not_called()


@pytest.mark.asyncio
async def test_plan_bare_shows_openshard_block_with_usage_hint(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/plan")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "OPENSHARD" in text
    assert "/plan" in text.lower()


@pytest.mark.asyncio
async def test_regression_ask_still_works_after_plan_added(tmp_path):
    app = _make_app(tmp_path)
    async with app.run_test(size=_SIZE) as pilot:
        ta = app.query_one("#task-input", TaskInput)
        ta.focus()
        ta.load_text("/ask what models do you support?")
        await pilot.press("enter")
        text = _text(app.query_one("#output-content", Static))
    assert "OPENSHARD" in text


@pytest.mark.asyncio
async def test_regression_normal_task_still_invokes_runner_after_plan_added(tmp_path):
    app = _make_app(tmp_path)
    with patch("openshard.tui.app.CliRunner") as mock_runner_cls:
        mock_runner_cls.return_value.invoke.return_value = _cli_ok()
        async with app.run_test(size=_SIZE) as pilot:
            ta = app.query_one("#task-input", TaskInput)
            ta.focus()
            ta.load_text("fix the auth bug")
            await pilot.press("enter")
            await pilot.pause(delay=0.3)
    assert mock_runner_cls.return_value.invoke.called
    assert "run" in str(mock_runner_cls.return_value.invoke.call_args_list[0])
