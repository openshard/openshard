"""``openshard ci gate``: the merge gate for one commit (docs/merge-gate.md).

Reads the agent marks on the pull request's commits (``ci/agent_marks.py``)
and this runner's own trusted evidence, asks the Openshard Platform for the
commit's gate verdict (``ci/gate_platform.py``), waiting while verification
is pending or while an agent change has no Receipt yet, and reduces them with
``ci/merge_gate.decide`` to pass or fail. In GitHub Actions it writes the
job summary and outputs. Nothing runs and nothing is written to the
history. Exit 0 pass, 1 fail; there is no third outcome.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import click

UNTRUSTED_HISTORY = "untrusted_history"
_EVIDENCE_FILES = ("runs.jsonl", "verifications.jsonl")
_ZERO_SHA = "0" * 40
POLL_SECONDS = 15.0


def _tracked_history() -> bool:
    """True when the history the verdict reads could have come with the change (or git cannot tell).

    Untrusted when the history directory or either evidence file is a symlink,
    or when git tracks the directory, either file, or the real path either
    file resolves to. Paths are matched case-insensitively (``:(icase)``) so a
    committed ``.OpenShard/`` on a case-insensitive filesystem counts too.
    """
    from openshard.history.locate import locate_history
    from openshard.util.git import run_git

    try:
        loc = locate_history(with_identity=False)
    except Exception:
        return False
    root, history = loc.root, loc.runs_path.parent
    files = [history / name for name in _EVIDENCE_FILES]
    try:
        if any(path.is_symlink() for path in (history, *files)):
            return True
        real_root = os.path.realpath(root)
        paths = {os.path.relpath(history, root)}
        for path in files:
            rel = os.path.relpath(os.path.realpath(path), real_root)
            if not rel.startswith(os.pardir):
                paths.add(rel)
    except (OSError, ValueError):
        return True
    out = run_git(root, ["ls-files", "--", *sorted(f":(icase){Path(x).as_posix()}" for x in paths)])
    if out is None:
        return (root / ".git").exists()
    return bool(out.strip())


def _local_verdict(sha: str) -> str | None:
    """``verified`` / ``failed`` from this runner's own trusted history, else None. Never raises."""
    try:
        from openshard.cli.verdict_cmd import _answer

        doc = _answer(sha, None)
        if doc.get("verdict") in ("verified", "failed") and not _tracked_history():
            return str(doc["verdict"])
    except Exception:
        pass
    return None


def _event(env) -> dict:
    """The GitHub Actions event payload, or {} outside GitHub Actions. Never raises."""
    path = env.get("GITHUB_EVENT_PATH")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _event_base(event: dict) -> str | None:
    """The commit the change starts from: the PR base, the merge queue base, or the push's ``before``."""
    from openshard.ci.gate_platform import is_sha

    for block, key in (("pull_request", "base"), ("merge_group", None)):
        raw = event.get(block)
        if isinstance(raw, dict):
            value = (raw.get(key) or {}).get("sha") if key else raw.get("base_sha")
            if is_sha(value):
                return str(value)
    before = event.get("before")
    return str(before) if is_sha(before) and before != _ZERO_SHA else None


def _is_fork(event: dict) -> bool:
    pr = event.get("pull_request")
    if not isinstance(pr, dict):
        return False
    head = ((pr.get("head") or {}).get("repo") or {}).get("full_name")
    base = ((pr.get("base") or {}).get("repo") or {}).get("full_name")
    return isinstance(head, str) and isinstance(base, str) and head != base


def _commit_marks(root: Path, head: str, base: str | None, pr_body: object) -> tuple[list[str] | None, str | None]:
    """Agent marks on the change's commits (and PR body), None when the commits could not be read,
    and the agent they name (for progress output only)."""
    from openshard.ci.agent_marks import agent_marks, agent_name
    from openshard.util.git import run_git

    body = pr_body if isinstance(pr_body, str) else ""
    args = ["log", "--format=%B%x00", f"{base}..{head}"] if base else ["log", "-1", "--format=%B", head]
    messages = run_git(root, args)
    if messages is None:
        return None, agent_name(body)
    marks = agent_marks(messages)
    for name in agent_marks(body):
        if name not in marks:
            marks.append(name)
    return marks, agent_name(messages) or agent_name(body)


def _change_findings(root: Path, base: str | None, head: str, pr: object) -> list[dict] | None:
    """The PR findings for base..head (with registry publish dates), or None when not checked. Never raises."""
    from openshard.verification.pr_findings import pr_findings

    title = pr.get("title") if isinstance(pr, dict) and isinstance(pr.get("title"), str) else None
    try:
        found = pr_findings(root, base, head, title=title, registry_lookup=True) if base else None
    except Exception:
        return None
    return [f.to_dict() for f in found] if found is not None else None


def _resolve(root: Path, ref: str) -> str | None:
    from openshard.ci.gate_platform import is_sha
    from openshard.cli.verdict_cmd import _resolve_commit

    sha = _resolve_commit(root, ref)
    return sha if is_sha(sha) else None


def _platform_verdict(api: str, sha: str, env, *, wait_minutes: float, poll_seconds: float,
                      wait_on_none: bool = False, progress=None):
    import httpx

    from openshard.ci import gate_platform as gp

    creds = gp.credentials_from_env(env)
    if isinstance(creds, gp.PlatformError):
        return creds
    with httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False) as http:
        return gp.poll(lambda: gp.fetch_once(api, sha, creds, http),
                       wait_seconds=wait_minutes * 60, interval=poll_seconds, wait_on_none=wait_on_none,
                       on_wait=progress.observe if progress else None,
                       sleep=progress.sleep if progress else time.sleep)


@click.command("gate")
@click.option("--commit", "commit_ref", required=True, metavar="SHA",
              help="The commit to gate: for a pull request, its head commit.")
@click.option("--base", "base_ref", default=None, metavar="SHA",
              help="Where the change starts (default: from the GitHub event). Its commits are checked for agent marks.")
@click.option("--api", default=None, metavar="URL",
              help="Openshard Platform API base (default: $OPENSHARD_GATE_API, else https://api.openshard.dev).")
@click.option("--wait-minutes", type=click.FloatRange(min=0, max=360), default=15.0, show_default=True,
              help="How long to wait while verification is pending, or while an agent change has no Receipt yet, "
                   "before failing.")
@click.option("--poll-seconds", type=click.FloatRange(min=0.1), default=POLL_SECONDS, hidden=True)
@click.option("--strict", is_flag=True, default=False, hidden=True,
              help="Deprecated, no effect: the gate only passes or fails.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
@click.option("--github-step-summary", "step_summary", is_flag=True, default=False,
              help="Append the gate summary to $GITHUB_STEP_SUMMARY.")
@click.option("--github-output", "github_output", is_flag=True, default=False,
              help="Write openshard_gate_status and openshard_gate_verdict to $GITHUB_OUTPUT.")
def ci_gate(commit_ref: str, base_ref: str | None, api: str | None, wait_minutes: float, poll_seconds: float,
            strict: bool, as_json: bool, step_summary: bool, github_output: bool) -> None:
    """Gate a merge on the commit's Receipt in the Openshard Platform: pass or fail, nothing in between.

    Pass when the Platform says the commit was verified, or when it has no
    Receipt, no commit carries an agent mark and the team only requires
    Receipts for agent changes. Fail otherwise, including when the Platform
    cannot be reached or refuses the credentials. Credentials: a GitHub
    Actions OIDC token (the job needs `id-token: write`), or
    $OPENSHARD_GATE_API_KEY with $OPENSHARD_GATE_ORG.
    """
    from openshard.ci import gate_platform as gp
    from openshard.ci.merge_gate import decide
    from openshard.cli.main import (
        _machine_envelope,
        _write_github_output,
        _write_github_step_summary,
    )
    from openshard.cli.ui.gate_wait import GateWait
    from openshard.history.locate import locate_history

    env = dict(os.environ)
    if strict:
        click.echo("warning: --strict is deprecated and has no effect: the gate only passes or fails.", err=True)
    try:
        root = locate_history(with_identity=False).root
    except Exception:
        root = Path.cwd()
    event = _event(env)
    if _is_fork(event):
        env["OPENSHARD_GATE_FORK"] = "true"
    api_base = (api or env.get("OPENSHARD_GATE_API") or gp.DEFAULT_API).strip()

    sha = _resolve(root, commit_ref)
    findings: list[dict] | None = None
    marks: list[str] | None = None
    local: str | None = None
    wait_on_none = False
    progress: GateWait | None = None
    platform: dict | gp.PlatformError
    if sha is None:
        platform = gp.PlatformError("error", f"{commit_ref!r} is not a full commit id in this repository.")
    elif not api_base.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        platform = gp.PlatformError("error", "The Platform API base must be an https:// URL.")
    else:
        # Read before asking: an agent change's Receipt is often still being
        # created (Cloud Receipts runs its checks first), so "none" on such a
        # change means keep asking within --wait-minutes. A human change, or
        # one this runner's own evidence decides, is decided at once.
        local_now = _local_verdict(sha)
        base = base_ref or _event_base(event)
        pr = event.get("pull_request")
        findings = _change_findings(root, base, sha, pr)
        marks_now, agent = _commit_marks(root, sha, base, pr.get("body") if isinstance(pr, dict) else None)
        wait_on_none = bool(marks_now) and local_now not in ("verified", "failed")
        progress = GateWait(agent=agent, wait_minutes=wait_minutes, env=env)
        platform = _platform_verdict(api_base, sha, env, wait_minutes=wait_minutes, poll_seconds=poll_seconds,
                                     wait_on_none=wait_on_none, progress=progress)
        if isinstance(platform, dict) and platform.get("status") == "none":
            local, marks = local_now, marks_now

    result = decide(sha or commit_ref, platform, local_verdict=local, agent_marks=marks,
                    waited_minutes=wait_minutes if wait_on_none else None, change_findings=findings)
    if progress is not None:
        progress.close("passed" if result.exit_code == 0 else "failed", result.verdict)

    if step_summary and not _write_github_step_summary(result.summary_markdown)[0]:
        click.echo("warning: GITHUB_STEP_SUMMARY is not set; step summary not written.", err=True)
    if github_output and not _write_github_output(
        {"openshard_gate_status": result.status, "openshard_gate_verdict": result.verdict}
    )[0]:
        click.echo("warning: GITHUB_OUTPUT is not set; outputs not written.", err=True)

    if as_json:
        click.echo(json.dumps(_machine_envelope(
            "ci gate", result.status, exit_code=result.exit_code, commit=sha, verdict=result.verdict,
            headline=result.headline, reasons=result.reasons, agent_marks=marks, local_verdict=local,
            platform=platform if isinstance(platform, dict) else None,
            change_findings=findings,
        ), indent=2))
    else:
        click.echo(f"Openshard gate: {result.status}")
        click.echo(result.headline)
    sys.exit(result.exit_code)


__all__ = ["ci_gate"]
