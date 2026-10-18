"""``openshard verify --signature``, ``openshard keys`` and the doctor's signing block.

Everything here runs nothing and needs no network: it reads seals, bundles
and public keys and says, in plain words, what is cryptographically bound
and what is only claimed (docs/signing.md). Private key material is never
printed, exported or written anywhere but the key file.
"""

from __future__ import annotations

import functools
import json
import sys
from pathlib import Path

import click

_W = 11  # label column width

_SOURCE_WORDS = {
    "this_install": "this install",
    "this_install_retired": "this install, retired key",
    "pinned": "pinned key",
    "bundle_only": "key carried in the bundle, not trusted here",
}

_NOT_PROVEN = "the agent's claims, the check results, or capture completeness"


def _row(label: str, text: str, width: int = _W) -> str:
    return f"{label:<{width}}{text}"


def _short(keyid: str | None) -> str:
    from openshard.history.receipt_keys import fingerprint8

    return f"ed25519:{fingerprint8(keyid)}…" if keyid else "an unnamed key"


def _sha(value: str | None) -> str:
    return f"{value[:4]}…" if value else "?"


# ---------------------------------------------------------------------------
# openshard verify --signature
# ---------------------------------------------------------------------------


def _headline(r) -> str:
    sealed = f"sealed by {_short(r.keyid)} ({_SOURCE_WORDS.get(r.key_source or '', 'unknown key')}) at {r.sealed_at}"
    if r.verdict == "verified":
        return f"verified — {sealed}" + (f"; key since revoked ({r.revoked_at})" if r.revoked_at else "")
    if r.verdict == "signed_unknown_key":
        if not r.signature_checked:
            return f"signed, not checked — the seal names {_short(r.keyid)}, whose public key is not available here"
        return f"signed, key not trusted here — {sealed}"
    if r.verdict == "not_signed":
        head = "Checksum only (not signed)" if r.content_hash_status == "valid" else "Not signed"
        if r.previously_sealed:
            return f"{head} — earlier seals exist, but the latest change was not sealed ({r.reason})"
        return f"{head} — " + ("no seal for this receipt" if r.reason == "no_seal" else f"not sealed ({r.reason})")
    return "invalid — " + _invalid_words(r)


def _invalid_words(r) -> str:
    words = {
        "record_changed": f"record changed after it was sealed (sealed {_sha(r.sealed_record_sha256)}, "
                          f"now {_sha(r.current_record_sha256)})",
        "projection_changed": f"receipt view changed after it was sealed (sealed {_sha(r.sealed_projection_sha256)}, "
                              f"now {_sha(r.current_projection_sha256)})",
        "bad_signature": f"the latest seal does not verify with {_short(r.keyid)}",
        "malformed_envelope": "the latest seal is malformed",
        "chain_broken": "seal chain broken: " + "; ".join(r.chain_problems),
        "key_revoked": f"sealed by {_short(r.keyid)} after that key was revoked ({r.revoked_at})",
        "receipt_id_mismatch": "the seal is for a different receipt",
        "malformed_statement": "the latest seal is malformed",
        "synced_receipt_changed": "the hosted receipt differs from the sealed copy",
        "reveal_mismatch": "a revealed field does not match what was sealed",
    }
    if r.reason.startswith("earlier_seal_"):
        return f"an earlier seal does not verify ({r.reason[len('earlier_seal_'):].replace('_', ' ')})"
    return words.get(r.reason, f"unknown seal format ({r.reason})")


def _detail_lines(r) -> list[str]:
    lines: list[str] = []
    record = {
        "matches": "matches the sealed content (record digest checked)",
        "changed": f"differs from the sealed content (sealed {_sha(r.sealed_record_sha256)}, "
                   f"now {_sha(r.current_record_sha256)})",
        "not_provided": "full record not provided; record digest not checked",
    }.get(r.record_check)
    if record and r.verdict != "not_signed":
        lines.append(_row("Record", record))
    projection = {
        "matches": "matches the sealed copy (projection digest checked)",
        "changed": f"differs from the sealed copy (sealed {_sha(r.sealed_projection_sha256)}, "
                   f"now {_sha(r.current_projection_sha256)})",
    }.get(r.projection_check)
    if projection and r.verdict != "not_signed":
        lines.append(_row("Projection", projection))
    if r.chain and r.signature_checked and not r.chain_problems:
        reasons = ", ".join(str(s.get("seal_reason")) for s in r.chain)
        many = f"{len(r.chain)} seals, each linked to the one before" if len(r.chain) > 1 else "1 seal"
        lines.append(_row("Chain", f"{many} ({reasons})"))
    if r.seal_version == "2" and r.signature_checked and r.verdict != "not_signed":
        hosted = "; it also names the receipt copy sent to the Platform" if r.sync_sealed else ""
        lines.append(_row("Fields", "every stored field is committed with a salt kept on the sealing machine, "
                                    "so one can be revealed later without the rest" + hosted))
    for item in r.reveals:
        check = item.get("check")
        lines.append(_row("Revealed", f"{item.get('field')}: " + (
            "matches its sealed commitment" if check == "matches"
            else f"does not match what was sealed ({str(check).replace('_', ' ')})")))
    return lines


def _meaning_lines(r) -> list[str]:
    own = r.key_source in ("this_install", "this_install_retired")
    what = "the receipt view" if r.record_check == "not_provided" else "the record"
    if r.verdict == "verified":
        means = f"{what} can't be changed afterwards without this check failing, except by someone holding this key"
    elif r.verdict == "signed_unknown_key" and r.signature_checked:
        means = ("unchanged since that key sealed it, but nothing here says whose key it is; "
                 "to trust it, get the key out of band and run `openshard keys trust FILE`")
    elif r.verdict == "signed_unknown_key":
        means = "nothing about this seal could be checked"
    elif r.verdict == "not_signed":
        means = ("no key binds this record; its checksum (content_hash) catches accidental edits, "
                 "but anyone who can edit the file can recompute it")
    else:
        means = "do not rely on this receipt: its content or its seals are not what was sealed"
    lines = [_row("Means", means)]
    if r.signature_checked and r.verdict != "not_signed":
        lines.append(_row("Claimed", "sealed_at, seal reason and Core version are signed but self-asserted "
                                     "(sealed_at is the sealing machine's clock)"))
    if own and r.verdict != "not_signed":
        lines.append(_row("Limit", "a coding agent running as your OS user can read this key and seal a forged "
                                   "record; a copy shared earlier (a bundle, a CI artefact) is what catches a "
                                   "later rewrite"))
    lines.append(_row("Not proven", _NOT_PROVEN))
    return lines


def render_verdict(r, origin: str) -> list[str]:
    """The text ``openshard verify --signature`` prints for one verdict."""
    lines = [_row("Receipt", f"{r.receipt_id or '?'} ({origin})"), _row("Signature", _headline(r))]
    lines += _detail_lines(r)
    if r.verdict == "not_signed" and r.content_hash_status:
        lines.append(_row("Checksum", f"content_hash {r.content_hash_status}"))
    return lines + _meaning_lines(r)


def _fail(as_json: bool, status: str, message: str, code: int = 1):
    from openshard.cli.main import _machine_envelope

    if as_json:
        click.echo(json.dumps(_machine_envelope("verify", status, signature_check=True, message=message), indent=2))
    else:
        click.echo(message)
    sys.exit(code)


def _local_target(receipt_ref: str | None, as_json: bool):
    from openshard.cli.main import _load_run_entries, _locate_history

    loc = _locate_history()
    entries = _load_run_entries(loc.runs_path)
    if receipt_ref:
        matches = [i for i, e in enumerate(entries)
                   if receipt_ref in (e.get("receipt_id"), e.get("shard_id"), e.get("run_id"))]
        index = matches[-1] if matches else None
    else:
        index = len(entries) - 1 if entries else None
    if index is None:
        _fail(as_json, "not_found", f"No receipt matches {receipt_ref!r}." if receipt_ref
              else "No receipt to check yet: run or capture an agent task first.")
    return loc, entries[index], index  # type: ignore[index]


def _write_bundle(path: str, bundle: dict, as_json: bool) -> None:
    try:
        Path(path).write_text(json.dumps(bundle, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        _fail(as_json, "error", f"Could not write the bundle to {path}: {exc.strerror or exc}")


def run_verify_signature(*, receipt_ref: str | None, bundle_file: str | None, bundle_out: str | None,
                         include_record: bool, as_json: bool, strict: bool, reveal: tuple[str, ...] = ()) -> None:
    """``openshard verify --signature``: check seals offline; run nothing."""
    from openshard.cli.main import _machine_envelope
    from openshard.history import receipt_verify as rv

    bundle_note = None
    if bundle_file:
        try:
            data = json.loads(Path(bundle_file).read_text(encoding="utf-8"))
            result = rv.verify_bundle(data)
        except (OSError, ValueError) as exc:
            _fail(as_json, "error", f"Could not read bundle {bundle_file}: {exc}")
        origin = f"bundle {Path(bundle_file).name}"
    else:
        loc, entry, index = _local_target(receipt_ref, as_json)
        result = rv.verify_local(loc.runs_path, entry, index)
        origin = "this repository"
        if bundle_out:
            if not result.chain:
                _fail(as_json, "not_signed", f"Receipt {result.receipt_id} has no seal; a bundle would prove nothing.")
            try:
                bundle = rv.build_bundle(loc.runs_path, entry, index, include_record=include_record, reveal=reveal)
            except rv.BundleError as exc:
                _fail(as_json, "error", f"Could not reveal: {exc}")
            _write_bundle(bundle_out, bundle, as_json)
            bundle_note = _row("Bundle", (
                f"written to {bundle_out}: {len(bundle['seals'])} seal line(s), {len(bundle['public_keys'])} "
                "public key(s) and the receipt view as `openshard history --json` shows it, which includes the "
                "task text, file names, repository and branch; "
                + ("the full record is included" if include_record
                   else "the full record is not included (add --include-record)")
                + (f"; revealed with their salts: {', '.join(dict.fromkeys(reveal))}" if reveal else "")))
    code = (rv.STRICT_EXIT_CODES if strict else rv.EXIT_CODES)[result.verdict]
    if as_json:
        click.echo(json.dumps(_machine_envelope(
            "verify", "ok", receipt=result.receipt_id, signature_check=True, verdict=result.verdict,
            signature=result.to_dict(), strict=strict, exit_code=code,
            bundle={"path": bundle_out, "include_record": include_record} if bundle_note else None,
        ), indent=2))
    else:
        for line in render_verdict(result, origin):
            click.echo(line)
        if bundle_note:
            click.echo(bundle_note)
        if strict and code == 2:
            click.echo("Strict: not verified (exit 2).")
    if code:
        sys.exit(code)


_RUN_MODES = (("from_ci", "--ci"), ("commit_ref", "--commit"), ("from_observed", "--from-observed"),
              ("approve", "--approve"), ("dry_run", "--dry-run"), ("compare_base", "--compare-base"),
              ("unattended", "--auto"))


def signature_mode(command):
    """Add ``--signature`` (and its bundle options) to ``openshard verify``.

    Without them the command runs exactly as before; with them, nothing is
    re-run and :func:`run_verify_signature` answers instead.
    """
    @functools.wraps(command)
    def wrapped(*args, signature: bool = False, bundle_file: str | None = None, bundle_out: str | None = None,
                include_record: bool = False, reveal: tuple[str, ...] = (), **kwargs):
        if not (signature or bundle_file or bundle_out or include_record or reveal):
            return command(*args, **kwargs)
        if not signature:
            raise click.UsageError("--file, --bundle, --include-record and --reveal need --signature")
        clashes = [flag for name, flag in _RUN_MODES if kwargs.get(name)]
        if clashes:
            raise click.UsageError(f"--signature runs nothing; it cannot be combined with {', '.join(clashes)}")
        if bundle_file and (bundle_out or kwargs.get("receipt_ref") or include_record):
            raise click.UsageError("--file checks a bundle; it cannot be combined with --receipt or --bundle")
        if include_record and not bundle_out:
            raise click.UsageError("--include-record needs --bundle")
        if reveal and not bundle_out:
            raise click.UsageError("--reveal needs --bundle")
        return run_verify_signature(receipt_ref=kwargs.get("receipt_ref"), bundle_file=bundle_file,
                                    bundle_out=bundle_out, include_record=include_record,
                                    as_json=bool(kwargs.get("as_json")), strict=bool(kwargs.get("strict")),
                                    reveal=tuple(reveal))

    wrapped.__dict__.pop("__click_params__", None)
    wrapped.__click_params__ = list(getattr(command, "__click_params__", []))  # type: ignore[attr-defined]
    for decorate in (
        click.option("--reveal", "reveal", multiple=True, metavar="FIELD",
                     help="With --bundle: disclose one stored field of the record, with its salt, so anyone "
                          "can check it against the seal's commitment (seal v2). Repeatable."),
        click.option("--include-record", is_flag=True, default=False,
                     help="With --bundle: also put the full record, every field as stored, in the bundle."),
        click.option("--bundle", "bundle_out", default=None, metavar="OUT.json",
                     help="With --signature: write a bundle anyone can check offline with --file: the seals, "
                          "public keys and the receipt view `history --json` shows (it includes the task text "
                          "and file names); the full record only with --include-record."),
        click.option("--file", "bundle_file", default=None, metavar="BUNDLE.json",
                     help="With --signature: check a bundle instead of this repository's history."),
        click.option("--signature", is_flag=True, default=False,
                     help="Run nothing: check the Receipt's seals offline and say what they prove "
                          "(verified, not_signed, signed_unknown_key or invalid; exit 1 when invalid)."),
    ):
        wrapped = decorate(wrapped)
    return wrapped


# ---------------------------------------------------------------------------
# openshard keys
# ---------------------------------------------------------------------------


@click.group("keys")
def keys_group() -> None:
    """The key that seals this install's Receipts, and keys you trust.

    Only public keys are ever shown or exported. A coding agent running as
    your OS user can read the private key; see docs/signing.md.
    """


def _key_dict(key) -> dict:
    from openshard.history.receipt_keys import fingerprint8

    return {"keyid": key.keyid, "fingerprint": fingerprint8(key.keyid), "source": key.source,
            "retired_at": key.retired_at, "revoked_at": key.revoked_at}


@keys_group.command("show")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def keys_show(as_json: bool) -> None:
    """Show this install's public key fingerprint and every key trusted here."""
    from openshard.history import receipt_keys as rk

    ring = rk.keyring()
    own = next((k for k in ring.values() if k.source == rk.SOURCE_THIS_INSTALL), None)
    if as_json:
        from openshard.cli.main import _machine_envelope

        click.echo(json.dumps(_machine_envelope(
            "keys", "ok" if own else "not_found",
            key={**_key_dict(own), **rk.public_key_record(own.keyid, own.public_raw)} if own else None,
            trusted=[_key_dict(k) for k in ring.values() if k is not own],
        ), indent=2))
        return
    if own is None:
        click.echo("No signing key yet: one is created when the first Receipt is sealed.")
    else:
        click.echo(_row("Key", f"{own.keyid} (this install)", 13))
        click.echo(_row("Fingerprint", rk.fingerprint8(own.keyid), 13))
        click.echo(_row("Public key", rk.public_key_record(own.keyid, own.public_raw)["public_key"], 13))
        click.echo(_row("Private key", f"{rk.keys_dir() / rk.PRIVATE_KEY_FILENAME} (never shown)", 13))
    for key in ring.values():
        if key is not own:
            extra = f", retired {key.retired_at}" if key.retired_at else ""
            extra += f", revoked {key.revoked_at}" if key.revoked_at else ""
            click.echo(_row("Trusted", f"{key.keyid} ({_SOURCE_WORDS.get(key.source, key.source)}{extra})", 13))


@keys_group.command("export")
@click.option("--output", "-o", default=None, metavar="FILE", help="Write to FILE instead of standard output.")
def keys_export(output: str | None) -> None:
    """Export this install's public key, for `openshard keys trust` elsewhere."""
    from openshard.history import receipt_keys as rk

    own = rk.read_public_key()
    if own is None:
        click.echo("No signing key yet: one is created when the first Receipt is sealed.")
        sys.exit(1)
    text = json.dumps(rk.public_key_record(*own), indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
        click.echo(f"Public key {own[0]} written to {output}.")
    else:
        click.echo(text, nl=False)


@keys_group.command("trust")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
def keys_trust(file: str) -> None:
    """Trust another install's exported public key, so its seals can read verified."""
    from openshard.history import receipt_keys as rk

    try:
        keyid = rk.trust_key(json.loads(Path(file).read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        click.echo(f"Not trusted: {exc}")
        sys.exit(1)
    click.echo(f"Trusted {keyid}. Make sure you got this file from its owner, not from the bundle it checks.")


@keys_group.command("rotate")
def keys_rotate() -> None:
    """Make a new signing key. Old seals keep verifying against the retired key."""
    from openshard.history import receipt_keys as rk

    try:
        old, new = rk.rotate_key()
    except rk.KeyUnavailableError as exc:
        click.echo(f"Not rotated ({exc.reason}); the current key was left as it is.")
        sys.exit(1)
    click.echo(f"New key {new}." + (f" Retired {old}; its seals still verify." if old else ""))
    click.echo("Records are not re-signed. Anyone who pinned the old key needs the new one: `openshard keys export`.")


@keys_group.command("revoke")
@click.argument("keyid")
def keys_revoke(keyid: str) -> None:
    """Revoke a retired or pinned key: its seals dated after now read invalid."""
    from openshard.history import receipt_keys as rk

    try:
        when = rk.revoke_key(keyid)
    except (ValueError, rk.KeyUnavailableError) as exc:
        click.echo(f"Not revoked: {exc}")
        sys.exit(1)
    click.echo(f"Revoked {keyid} at {when}. Seals dated earlier still verify, labelled as by a key since revoked; "
               "the date is the sealing machine's own clock.")


# ---------------------------------------------------------------------------
# openshard doctor
# ---------------------------------------------------------------------------


def doctor_state(base_dir: Path) -> dict:
    """The ``receipt_signing`` block of ``openshard doctor --json``. Never raises."""
    from openshard.history import receipt_keys as rk
    from openshard.history.receipt_signing import FAILURES_FILENAME, signing_enabled
    from openshard.util.home import openshard_home

    try:
        state = rk.key_health()
        state["enabled"] = signing_enabled(Path(base_dir) / ".openshard")
        failures = Path(openshard_home()) / FAILURES_FILENAME
        state["home_failures_recorded"] = failures.is_file() and failures.stat().st_size > 0
    except Exception as exc:  # doctor reports; it never fails on this block
        return {"enabled": None, "problem": f"check_failed:{type(exc).__name__}"}
    return state


def doctor_checks(state: dict) -> list[tuple[str, bool, str]]:
    """``(label, ok, detail)`` rows for the doctor's Receipt signing block."""
    from openshard.history.receipt_keys import fingerprint8

    if state.get("enabled") is None:
        return [("Receipt signing", False, f"could not be checked ({state.get('problem')})")]
    rows = [("Signing on", bool(state["enabled"]), "turned off (OPENSHARD_RECEIPT_SIGNING or receipt_signing: false)")]
    if not state.get("crypto_available"):
        return rows + [("Crypto library", False, "`cryptography` is not installed; Receipts are not signed")]
    if not state.get("key_present"):
        return rows + [("Signing key: none yet (created at the first seal)", True, "")]
    if state.get("problem"):
        return rows + [("Signing key", False, f"{state['problem']}; Receipts are recorded as not signed")]
    rows.append((f"Signing key {fingerprint8(state['keyid'])}", True, ""))
    if state.get("platform") == "windows":
        rows.append(("Key owner-only", bool(state.get("owner_only")),
                     "the owner-only ACL could not be confirmed (icacls); other accounts on this machine "
                     "may be able to read the key"))
    else:
        rows.append(("Key owner-only", bool(state.get("owner_only")),
                     "the key file is readable by other users; it is tightened to 0600 at the next seal"))
    if state.get("home_failures_recorded"):
        rows.append(("Signing failures", False, "some seals could not be written; see "
                     "~/.openshard/receipt-signing-failures.jsonl"))
    return rows


def echo_doctor_block(state: dict) -> None:
    """Print the Receipt signing block, then the blank line before doctor's verdict."""
    click.echo("\nReceipt signing\n")
    for label, ok, detail in doctor_checks(state):
        click.echo(f"  {'✓' if ok else '✗'} {label}" + ("" if ok else f" ({detail})"))
    click.echo("  A coding agent running as your OS user can read the key; see docs/signing.md.")
    click.echo("")


__all__ = ["doctor_checks", "doctor_state", "echo_doctor_block", "keys_group", "render_verdict",
           "run_verify_signature", "signature_mode"]
