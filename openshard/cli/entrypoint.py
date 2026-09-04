"""Console-script entrypoint for the ``openshard`` command (PR7).

Split out of :mod:`openshard.cli.main` so the two latency-sensitive Claude
Code capture commands -- ``openshard hooks claude`` and ``openshard hooks
claude-status`` -- never pay the cost of importing the full CLI.

Why this exists
----------------
``openshard.cli.main`` is a single Click app; building it eagerly imports
the run pipeline, provider clients (``httpx`` and friends), the planning
generator, and the evals runner -- none of which the hook/status-line
commands touch. On this project's dev machine that import alone measured
~900ms, and Claude Code spawns a *fresh process* for every hook and every
status-line render, with ``Stop``/``SessionEnd`` hooks and the status line
run *synchronously* (Claude Code waits on them). That makes the unrelated
import cost fully visible to the user on every turn.

This module recognizes the exact, fixed argv shapes those two commands use
and dispatches straight to :mod:`openshard.adapters.claude_capture_client`
(PR9.5: which forwards to the warm capture service, falling back to
:mod:`openshard.adapters.claude_hooks` in-process only when no service can
be reached), importing nothing else. Anything that is not an exact match -- including
``--help``, unknown flags, or any other subcommand -- falls straight
through to the real Click app (``openshard.cli.main:cli``), completely
unchanged. The fast path is a pure latency optimization: every behavior it
implements (stdin parsing, exit code, stdout/stderr discipline) mirrors the
Click commands in ``cli/main.py`` exactly, and the slow path is always
available as a fallback for anything the fast path does not recognize.
"""

from __future__ import annotations

import sys


def _parse_hooks_claude_argv(rest: list[str]) -> str | None | bool:
    """Return the ``--event`` override for ``hooks claude`` *rest* argv.

    Returns ``None`` if there is no override (still a fast-path match), or
    ``False`` (a sentinel, not a valid override) if *rest* does not match
    the exact syntax the real Click command accepts -- callers must fall
    through to the full CLI in that case.
    """
    if not rest:
        return None
    # Mirrors the real Click option exactly: only a long ``--event`` flag is
    # defined there (no short form), so that is all the fast path accepts.
    if len(rest) == 2 and rest[0] == "--event":
        return rest[1]
    if len(rest) == 1 and rest[0].startswith("--event="):
        return rest[0].split("=", 1)[1]
    return False


def _parse_hooks_codex_argv(rest: list[str]) -> tuple[str | None, bool] | None:
    """``(event_override, spawn)`` for ``hooks codex`` *rest* argv, or None to fall through.

    Accepts exactly what the Click command does: an optional ``--no-spawn``
    flag and an optional ``--event`` value, in either order.
    """
    spawn = True
    event: str | None = None
    args = list(rest)
    while args:
        head = args.pop(0)
        if head == "--no-spawn":
            spawn = False
        elif head == "--event" and args:
            event = args.pop(0)
        elif head.startswith("--event="):
            event = head.split("=", 1)[1]
        else:
            return None
    return event, spawn


def _try_fast_path(argv: list[str]) -> bool:
    """Handle *argv* without importing ``openshard.cli.main``. Returns True if handled."""
    if len(argv) < 2:
        return False
    if argv[0] == "capture" and argv[1] == "serve" and len(argv) == 2:
        # The capture service itself (spawned detached by the client). It is
        # long-running, but skipping the full CLI import still shortens the
        # window between "service not running" and "service healthy".
        import os

        from openshard.adapters.claude_capture_service import serve

        raise SystemExit(serve(env=os.environ))
    if argv[0] != "hooks":
        return False
    sub, rest = argv[1], argv[2:]

    if sub == "claude":
        event_override = _parse_hooks_claude_argv(rest)
        if event_override is False:
            return False
        import os

        from openshard.adapters.claude_capture_client import run_hook_via_service

        run_hook_via_service(sys.stdin, env=os.environ, event_override=event_override)  # type: ignore[arg-type]
        return True

    if sub == "claude-status":
        if rest:
            return False
        import os

        from openshard.adapters.claude_capture_client import run_status_via_service

        sys.stdout.write(run_status_via_service(sys.stdin, env=os.environ) + "\n")
        sys.stdout.flush()
        return True

    if sub == "codex":
        # PR12: Codex only has command hooks, so every Codex event pays a
        # process start; keeping it on the fast path is what keeps that
        # start small.
        parsed = _parse_hooks_codex_argv(rest)
        if parsed is None:
            return False
        import os

        from openshard.adapters.claude_capture_client import run_hook_via_service

        run_hook_via_service(  # type: ignore[arg-type]
            sys.stdin, env=os.environ, event_override=parsed[0], agent="codex", spawn=parsed[1],
        )
        return True

    return False


def main() -> None:
    if _try_fast_path(sys.argv[1:]):
        return
    from openshard.cli.main import cli

    cli()


if __name__ == "__main__":
    main()
