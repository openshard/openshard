"""``openshard ci fail-without-fix``: do the change's new tests fail on the code before it?"""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.command("fail-without-fix")
@click.option("--base", "base_ref", required=True, metavar="REF", help="The branch or commit the change merges into.")
@click.option("--timeout", type=click.FloatRange(min=1), default=600.0, show_default=True,
              help="Seconds allowed for each pytest run.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def ci_fail_without_fix(base_ref: str, timeout: float, as_json: bool) -> None:
    """Run the Python tests HEAD adds or rewrites on BASE's merge-base, then on HEAD.

    Runs the change's own tests (pytest, this interpreter, nothing installed).
    Exit 0 when the comparison completed, 2 when it could not be established.
    """
    from openshard.verification.fail_without_fix import run_fail_without_fix

    result = run_fail_without_fix(Path.cwd(), base_ref, timeout=timeout)
    if as_json:
        click.echo(json.dumps({"schema": "openshard.fail_without_fix.v1", **result}, indent=2))
    elif result["status"] != "complete":
        click.echo(f"Fail-without-fix: not established ({result.get('reason', 'unknown')}).")
    else:
        c = result["counts"]
        click.echo(f"Fail-without-fix: {c['new_or_changed']} new or changed test(s): {c['proves_fix']} fail without "
                   f"the change, {c['passes_without_fix']} pass without it, {c['fails_on_head']} fail on head, "
                   f"{c['not_run']} not run.")
        for name in result["passes_without_fix_tests"]:
            click.echo(f"  passes without the change: {name}")
    raise SystemExit(0 if result["status"] == "complete" else 2)


__all__ = ["ci_fail_without_fix"]
