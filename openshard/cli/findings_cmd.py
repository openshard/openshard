"""``openshard ci findings``: the fixed PR findings for a change, for people and for scripts."""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.command("findings")
@click.option("--base", "base_ref", required=True, metavar="REF",
              help="The branch or commit the change will merge into (the PR base).")
@click.option("--head", "head_ref", default="HEAD", show_default=True, metavar="REF", help="The change's head.")
@click.option("--registry-lookup", is_flag=True, default=False,
              help="Also ask PyPI / npm when each new dependency was published. Sends the package names to the "
                   "public registry; 15 s in total. Off by default.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def ci_findings(base_ref: str, head_ref: str, registry_lookup: bool, as_json: bool) -> None:
    """Fixed, model-free findings for the change from BASE's merge-base to HEAD.

    Findings are observations, never a pass or fail: exit 0 when the change
    was checked (with or without findings), 2 when git could not say what
    changed (for example a shallow clone without the base).
    """
    from openshard.cli.gate_cmd import _resolve
    from openshard.verification.pr_findings import pr_findings

    root = Path.cwd()
    base, head = _resolve(root, base_ref), _resolve(root, head_ref)
    findings = pr_findings(root, base, head, registry_lookup=registry_lookup) if base and head else None
    if as_json:
        click.echo(json.dumps({
            "schema": "openshard.pr_findings.v1",
            "base": base, "head": head,
            "checked": findings is not None,
            "findings": [f.to_dict() for f in findings] if findings is not None else None,
        }, indent=2))
    elif findings is None:
        click.echo("Change checks: not checked (git could not read the change; fetch the base with fetch-depth: 0).")
    elif not findings:
        click.echo("Change checks: nothing found.")
    else:
        click.echo(f"Change checks: {len(findings)} finding{'' if len(findings) == 1 else 's'}.")
        for f in findings:
            paths = f" ({', '.join(f.paths)})" if f.paths else ""
            click.echo(f"  - {f.kind}: {f.detail}{paths}")
    raise SystemExit(0 if findings is not None else 2)


__all__ = ["ci_findings"]
