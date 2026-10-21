"""Progress while ``openshard ci gate`` waits for the Platform (docs/merge-gate.md).

Written to stderr, so the gate's stdout contract (``Openshard gate: pass`` and
the headline) is unchanged. Two renderings of the same fields:

* plain: one aligned, timestamped line per minute of waiting, used in CI,
  when stderr is not a terminal, or when ``NO_COLOR`` / ``TERM=dumb`` is set.
  In GitHub Actions the lines are folded in a ``::group::`` and the closing
  line, with the decision, is printed after it so it stays visible;
* rich: in an interactive terminal, a spinner that shows the same fields and
  a running elapsed time, replaced by the closing line when the wait ends.

Nothing here decides anything: it only reports what the poll saw. Never
raises.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import IO, Any

PREFIX = "Openshard gate"
SEP = "  ·  "
_PHASE_WIDTH = 24  # "waiting for verification"
_AGENT_WIDTH = 16
_STATE_WIDTH = 11  # "unreachable"

_PHASES = {
    "none": "waiting for receipt",
    "pending": "waiting for verification",
    "unreachable": "retrying the Platform",
}


def _fit(text: str, width: int) -> str:
    text = " ".join("".join(ch for ch in text if ch.isprintable()).split())
    return text if len(text) <= width else text[: width - 1] + "…"


def _minutes(value: float) -> str:
    return f"{value:g}m"


def use_rich(stream: Any, env: Mapping[str, str]) -> bool:
    """Rich only for a person at a terminal: a TTY, outside CI, colour allowed."""
    if env.get("NO_COLOR") or env.get("TERM") == "dumb" or env.get("GITHUB_ACTIONS") == "true" or env.get("CI"):
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


class GateWait:
    """Reports the gate's wait. ``observe`` each answer that leads to a wait, ``sleep`` between polls, then ``close``."""

    def __init__(
        self,
        *,
        agent: str | None,
        wait_minutes: float,
        stream: IO[str] | None = None,
        env: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.stream: IO[str] = stream if stream is not None else sys.stderr
        env = env if env is not None else {}
        self.agent = _fit(agent or "agent", _AGENT_WIDTH)
        self._named = bool(agent)
        self.total = wait_minutes
        self.github = env.get("GITHUB_ACTIONS") == "true"
        self.rich = use_rich(self.stream, env)
        self._monotonic, self._sleep, self._now = monotonic, sleep, now
        self._start: float | None = None
        self._state = "none"
        self._minute = -1
        self._status: Any = None
        self._console: Any = None

    # -- the fields ---------------------------------------------------------

    def _elapsed(self) -> float:
        return 0.0 if self._start is None else max(0.0, self._monotonic() - self._start)

    def line(self, phase: str, state: str, *, clock: bool = False) -> str:
        """One aligned line in whole minutes; with *clock*, the spinner's compact form with mm:ss."""
        elapsed = self._elapsed()
        if clock:
            total = round(self.total * 60)
            spent = f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d} / {total // 60:02d}:{total % 60:02d}"
            return " · ".join([PREFIX, phase, self.agent, state, spent])
        spent = f"{_minutes(int(elapsed // 60)):>3} / {_minutes(self.total)}"
        return SEP.join([
            PREFIX,
            f"{_fit(phase, _PHASE_WIDTH):<{_PHASE_WIDTH}}",
            f"{self.agent:<{_AGENT_WIDTH}}",
            f"{_fit(state, _STATE_WIDTH):<{_STATE_WIDTH}}",
            spent,
        ])

    def _stamp(self, text: str) -> str:
        return f"{self._now().strftime('%H:%M:%SZ')}  {text}"

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text + "\n")
            self.stream.flush()
        except Exception:
            pass

    # -- the wait -----------------------------------------------------------

    def observe(self, answer: object) -> None:
        """The answer the poll is about to wait on: a gate verdict dict or a transient PlatformError."""
        status = answer.get("status") if isinstance(answer, dict) else "unreachable"
        if not self._named and isinstance(answer, dict):
            named = [r.get("agent") for r in answer.get("receipts") or [] if isinstance(r, dict)]
            first = next((n for n in named if isinstance(n, str) and n.strip()), None)
            if first:
                self.agent, self._named = _fit(first, _AGENT_WIDTH), True
        self._state = status if status in _PHASES else "unreachable"
        if self._start is None:
            self._start = self._monotonic()
            self._open()
        self._tick()

    def _open(self) -> None:
        if self.rich:
            try:
                from rich.console import Console

                self._console = Console(file=self.stream, force_terminal=True, highlight=False, soft_wrap=True)
                self._status = self._console.status(self._spinner_text(), spinner="dots")
                self._status.start()
                return
            except Exception:
                self.rich = False
        if self.github:
            self._write(f"::group::{PREFIX}{SEP}waiting up to {_minutes(self.total)} for the Platform{SEP}{self.agent}")

    def _spinner_text(self) -> str:
        return self.line(_PHASES[self._state], self._state, clock=True)

    def _tick(self) -> None:
        if self.rich and self._status is not None:
            try:
                self._status.update(self._spinner_text())
            except Exception:
                pass
            return
        elapsed = self._elapsed()
        minute = int(elapsed // 60)
        if minute != self._minute and (minute == 0 or elapsed < self.total * 60):
            self._minute = minute
            self._write(self._stamp(self.line(_PHASES[self._state], self._state)))

    def sleep(self, seconds: float) -> None:
        """Sleep between polls in slices of at most a second, keeping the elapsed time current."""
        end = self._monotonic() + max(0.0, seconds)
        while True:
            left = end - self._monotonic()
            if left <= 0:
                return
            self._sleep(min(1.0, left))
            if self._start is not None:
                self._tick()

    def close(self, decision: str, verdict: str) -> None:
        """End the wait with the decision (``passed`` / ``failed``) and the Platform's final answer."""
        if self._start is None:
            return
        if self.rich and self._status is not None:
            final = self.line(decision, verdict, clock=True)
            try:
                self._status.stop()
                style = "bold green" if decision == "passed" else "bold red"
                self._console.print(final, style=style, markup=False, highlight=False)
                return
            except Exception:
                pass
        final = self.line(decision, verdict)
        if self.github:
            self._write("::endgroup::")
        self._write(self._stamp(final))


__all__ = ["GateWait", "use_rich"]
