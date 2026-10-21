"""The merge gate's wait progress (``cli/ui/gate_wait.py``): plain lines in CI, a spinner at a terminal."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta

import pytest

from openshard.ci import gate_platform as gp
from openshard.cli.ui.gate_wait import GateWait, use_rich

START = datetime(2026, 10, 21, 10, 0, 20, tzinfo=UTC)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, s: float) -> None:
        self.now += s


def _wait(stream, env, *, agent: str | None = "Codex", minutes: float = 15):
    clock = _Clock()
    g = GateWait(agent=agent, wait_minutes=minutes, stream=stream, env=env, monotonic=lambda: clock.now,
                 sleep=clock.sleep, now=lambda: START + timedelta(seconds=clock.now))
    return g, clock


def _run(g: GateWait, states: list[str], interval: float = 15) -> None:
    for state in states:
        g.observe({"status": state, "receipts": []})
        g.sleep(interval)


class TestMode:
    @pytest.mark.parametrize("env,tty,rich", [
        ({}, True, True),
        ({}, False, False),
        ({"NO_COLOR": "1"}, True, False),
        ({"TERM": "dumb"}, True, False),
        ({"GITHUB_ACTIONS": "true"}, True, False),
        ({"CI": "true"}, True, False),
    ])
    def test_rich_only_for_a_person_at_a_terminal(self, env, tty, rich):
        assert use_rich(_Tty() if tty else io.StringIO(), env) is rich

    def test_a_stream_without_isatty_is_plain(self):
        assert use_rich(object(), {}) is False


class TestPlain:
    def test_github_actions_one_aligned_line_a_minute_folded_in_a_group(self):
        out = io.StringIO()
        g, _ = _wait(out, {"GITHUB_ACTIONS": "true"})
        _run(g, ["none"] * 13 + ["pending"] * 4)  # 17 polls, 15 s apart
        g.close("passed", "verified")
        lines = out.getvalue().splitlines()
        assert lines[0] == "::group::Openshard gate  ·  waiting up to 15m for the Platform  ·  Codex"
        assert lines[1] == ("10:00:20Z  Openshard gate  ·  waiting for receipt       ·  Codex             ·  "
                            "none         ·   0m / 15m")
        assert lines[5] == ("10:04:20Z  Openshard gate  ·  waiting for verification  ·  Codex             ·  "
                            "pending      ·   4m / 15m")
        assert lines[6] == "::endgroup::"
        assert lines[7] == ("10:04:35Z  Openshard gate  ·  passed                    ·  Codex             ·  "
                            "verified     ·   4m / 15m")
        assert len(lines) == 8  # once a minute, not every poll
        body = [line.split("Z  ", 1)[1] for line in lines[1:6] + lines[7:]]
        assert len({len(line) for line in body}) == 1  # columns line up
        assert "..." not in out.getvalue() and "\x1b" not in out.getvalue()

    def test_no_line_at_the_deadline_only_the_decision(self):
        out = io.StringIO()
        g, _ = _wait(out, {"GITHUB_ACTIONS": "true"}, agent=None, minutes=2)
        _run(g, ["none"] * 8)
        g.close("failed", "none")
        lines = out.getvalue().splitlines()
        assert [line.rsplit("·", 1)[1].strip() for line in lines[1:-2]] == ["0m / 2m", "1m / 2m"]
        assert lines[-2] == "::endgroup::" and "failed" in lines[-1] and lines[-1].endswith("2m / 2m")
        assert "agent" in lines[-1]  # nothing named an agent: a neutral word, never a guess

    def test_outside_github_no_group_markers(self):
        out = io.StringIO()
        g, _ = _wait(out, {})
        _run(g, ["pending"] * 2)
        g.close("failed", "timeout")
        text = out.getvalue()
        assert "::group::" not in text and "::endgroup::" not in text
        assert text.splitlines()[-1].endswith("·  timeout      ·   0m / 15m")

    def test_no_color_at_a_terminal_falls_back_to_plain_lines(self):
        out = _Tty()
        g, _ = _wait(out, {"NO_COLOR": "1"})
        _run(g, ["none"])
        g.close("passed", "verified")
        text = out.getvalue()
        assert "\x1b" not in text and text.splitlines()[0].startswith("10:00:20Z  Openshard gate  ·  waiting")

    def test_transient_errors_read_as_retrying(self):
        out = io.StringIO()
        g, _ = _wait(out, {})
        g.observe(gp.PlatformError(gp.ERROR_UNREACHABLE, "down", transient=True))
        assert "retrying the Platform" in out.getvalue() and "unreachable" in out.getvalue()

    def test_the_agent_falls_back_to_the_receipt_and_is_bounded(self):
        out = io.StringIO()
        g, _ = _wait(out, {}, agent=None)
        g.observe({"status": "pending", "receipts": [{"agent": "A very long agent name\x1b[31m indeed"}]})
        line = out.getvalue().splitlines()[0]
        assert "A very long age…" in line and "\x1b" not in line

    def test_nothing_is_written_when_the_gate_never_waited(self):
        out = io.StringIO()
        g, _ = _wait(out, {"GITHUB_ACTIONS": "true"})
        g.close("passed", "verified")
        assert out.getvalue() == ""

    def test_sleep_ticks_in_slices_of_a_second(self):
        out = io.StringIO()
        g, clock = _wait(out, {})
        g.observe({"status": "none"})
        g.sleep(15)
        assert clock.now == 15


class TestRich:
    def test_a_terminal_gets_a_spinner_and_the_decision_line(self):
        out = _Tty()
        g, _ = _wait(out, {})
        _run(g, ["none", "none", "pending"])
        g.close("passed", "verified")
        text = out.getvalue()
        assert "\x1b[" in text  # terminal control, not plain lines
        assert "::group::" not in text and "10:00:20Z" not in text
        assert "Openshard gate · waiting for " in text and "/ 15:00" in text
        assert "Openshard gate · passed · Codex · verified · 00:45 / 15:00" in text
        assert "\n·" not in text  # one line each, never wrapped mid-field
