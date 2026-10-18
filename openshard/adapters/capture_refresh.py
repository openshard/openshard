"""Replace an out-of-date capture service during ``openshard setup`` / ``login``, and say what ran.

A capture service keeps the code it was started with. After an upgrade,
setup used to report ``Capture: running`` for the old service, which went on
folding sessions without the upgrade's fixes (line counts, signing) until
it idled out. Here setup asks such a service to drain and exit (the same
graceful path as ``openshard capture stop``: queued events are folded first,
and anything still queued is replayed by the next start), starts this
installation's service, and reports the restart with the version it
replaced. Only a service that provably belongs to this installation is
asked; another user's or another home's service is never touched (see
``claude_capture_client.service_is_ours``).

The display helpers below are shared by ``setup``, ``login`` and
``capture status`` so the three say the same thing.
"""

from __future__ import annotations

import os

from openshard.adapters import claude_capture_client as client

# Long enough for a drain that is folding a session (stop uses the same).
_DRAIN_WAIT_SECONDS = 30.0

# Two commands, not `a && b`: Windows PowerShell 5 has no `&&`.
RESTART_COMMAND = "`openshard capture stop`, then `openshard capture start`"


def _label(version: object, build: object) -> str:
    text = str(version) if isinstance(version, str) and version else "unknown version"
    return f"{text} (build {build})" if isinstance(build, str) and build else text


def _running_and_installed(status: dict) -> tuple[str, str]:
    """The running and installed code, with builds only when the versions alone look the same."""
    from openshard import __version__

    version = status.get("version")
    if version != __version__:
        return _label(version, None), str(__version__)
    build = status.get("build")
    running = _label(version, build) if isinstance(build, str) and build else f"an earlier {version} build"
    return running, _label(__version__, client.installed_build())


def ensure_current_service(env: dict | os._Environ | None = None) -> dict:
    """Start (or find) the capture service, replacing one of ours that runs other code.

    Returns ``{"state", "port"}`` with state as :func:`client.ensure_service`
    reports it, or ``restarted`` (an out-of-date service of ours was drained
    and this installation's started) or ``restart_failed`` (it did not exit
    in time and is still running). Both carry ``previous_version``. Never raises.
    """
    env = os.environ if env is None else env
    previous: dict | None = None
    try:
        if not client.disabled(env):
            port = client.resolve_port(env)
            doc = client.health(port, env=env, credential=True)
            if doc is not None and client.service_is_ours(doc, env) is True and client.service_version_stale(doc):
                previous = {"previous_version": doc.get("version"), "previous_build": doc.get("build")}
                if not client.request_shutdown(env, wait_seconds=_DRAIN_WAIT_SECONDS):
                    return {"state": "restart_failed", "port": port, **previous}
        port_now, state = client.ensure_service(env)
    except Exception:
        port_now, state = None, "unavailable"
    if previous is not None and state in ("started", "running"):
        state = "restarted"
    return {"state": state, "port": port_now, **(previous or {})}


def setup_service_line(service: dict) -> str:
    """The ``Capture:`` value setup and login print for :func:`ensure_current_service`'s result."""
    state = str(service.get("state"))
    port = service.get("port")
    previous, _ = _running_and_installed(
        {"version": service.get("previous_version"), "build": service.get("previous_build")}
    )
    return {
        "running": f"running (127.0.0.1:{port})",
        "started": f"started (127.0.0.1:{port})",
        "restarted": f"restarted (was {previous}; 127.0.0.1:{port})",
        "restart_failed": (
            f"out of date (running {previous}); could not restart it -- run {RESTART_COMMAND}"
        ),
        "disabled": "disabled (OPENSHARD_CAPTURE_DISABLE)",
        "unavailable": "not running (could not be started)",
    }.get(state, state)


def status_version_line(status: dict) -> str | None:
    """``capture status``'s version line for a running service of ours, or None."""
    if not status.get("running") or status.get("credential") != "accepted":
        return None
    if not status.get("stale_version"):
        return f"  version:  {_label(status.get('version'), status.get('build'))}"
    running, installed = _running_and_installed(status)
    return f"  version:  {running} -- MISMATCH: installed Core is {installed}"


def stale_detail(status: dict) -> str:
    """Why a running service of ours is out of date, for ``capture status`` and ``doctor``."""
    running, installed = _running_and_installed(status)
    return (
        f"running OpenShard {running} while this installation is {installed}; it still folds "
        "sessions with the old code until replaced -- run `openshard setup` (or start a new session) to "
        "replace it now"
    )
