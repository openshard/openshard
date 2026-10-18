# Signed Receipts and the hosted witness

Status: the hosted witness ships in v0.5.0 (see "The hosted witness" below).
Signing Phase 1 implemented. The per-install key and sealing
(`history/receipt_keys.py`, `history/receipt_signing.py`, on by default);
the offline check, bundles and `signature_status`
(`history/receipt_verify.py`); `openshard verify --signature`,
`openshard keys` and the `doctor` block (`cli/signing_cmd.py`).
New seals are seal v2 ([receipt-seal-v2.md](receipt-seal-v2.md)): the
same envelope with field commitments and, on a seal made by sync, a digest
of the exact hosted receipt, so the Platform copy can be checked against
the install's key (pulled into v0.5.0 from Phase 2). v1 seals keep
verifying. Phase 3 is not started.

**The limit, stated plainly:** a seal made with this machine's key proves
only that the record is unchanged since it was sealed with this machine's
key. A coding agent running as the same OS user can read that key and sign
a forged record with it (threat 1). Catching a later rewrite needs a party
the agent cannot reach: the hosted witness (v0.5.0, below).

Today every Receipt carries a `content_hash` (`history/shard_hash.py`): a
SHA-256 over the record's canonical JSON. It catches an edit that forgot to
update the hash. It does not stop anyone who can edit `.openshard/runs.jsonl`
from changing a record and recomputing the hash; the hash has no key. This
page proposes signatures that anyone can check offline, and says exactly
what they prove.

## What a signature proves, and what it does not

A signature is one narrow claim: *the holder of key K sealed a record whose
content hashed to H, and has not been shown to have changed it since.*

| It proves | It does not prove |
| --- | --- |
| The record was not changed after sealing by someone without the key. | That anything the agent said is true. Agent-reported evidence stays agent-reported. |
| Which install (key) sealed it. | That the checks passed. That is the verification block, which keeps its own evidence source (`agent_reported`, `directly_observed`, `independently_verified`). |
| That a later amendment (`note`, `feedback`, `verify`) was sealed by the same key, if it was. | That capture was complete. Missing evidence stays missing. |
| | When it was sealed. `signed_at` is the local clock, self-asserted, until the hosted witness adds its own time. |

Signing adds no evidence level. A signed `agent_reported` claim is still an
`agent_reported` claim. The independent re-run (`openshard verify`) and the
hosted witness are separate claims and are shown separately.

### Threats

1. **An agent edits local files.** Coding agents run as the developer's OS
   user, so they can read `~/.openshard/` and use the key. A local key does
   **not** stop a same-user agent from forging and signing a record before
   or instead of OpenShard. It does make a quiet after-the-fact rewrite
   detectable once a copy has left the machine (Platform, CI artefact, a
   shared bundle), because the rewrite needs a new seal and the new seal
   disagrees with the copy. We say this plainly in the docs and the CLI.
2. **The developer edits a record.** Same as above: possible with the key,
   detectable against any copy sealed earlier. Without the key (a teammate
   editing a committed or exported file), the edit is caught directly.
3. **Signature stripping.** Deleting the signature turns a record into
   "not signed", which is shown as weaker, never as failed. Locally this
   cannot be prevented. Phase 2 catches it: the Platform knows when an
   install registered its key and flags an unsigned Receipt from that
   install created after that time.
4. **Platform compromise.** An attacker with the Platform's database can
   delete or hide Receipts, but cannot forge an install's signature. With
   the Platform signing key they can forge *witness* statements only.
   Phase 3 (a public transparency log) is what limits that.
5. **Key theft.** Anyone with the private key can sign as that install.
   Rotation and revocation below; hardware-backed keys are out of scope.

## The hosted witness (v0.5.0)

A Receipt lives in `.openshard/runs.jsonl`, inside the repository the agent
can write. The witness is a copy the agent cannot rewrite: with remote or
connected capture, each Event leaves the machine seconds after it happens,
and the Platform, not the machine, records when it arrived and in what
order. The public promise is **proof that can't be changed afterwards**.

| The witness proves | It does not prove |
| --- | --- |
| The Platform received this Event, with this content, at its own time, and has not changed it since. | That the Event is true. An agent running as the developer's OS user can still send invented Events live, with the local credential. We say so. |
| An Event changed or deleted on the machine after it was acknowledged disagrees with the held copy. | That capture was complete. Events that reached the witness late (buffered offline) or never are shown as such, never as witnessed. |
| | That checks passed. "Witnessed" is never "verified"; independent verification stays CI or `openshard verify`. |

The witness adds no evidence level and never turns a result green.

What Core does:

- **Session nonce.** When a captured session starts, Core mints a random
  128-bit nonce (26 lowercase base32 characters, URL-safe). It is not a
  secret. It rides in the metadata of the session's first remote-capture
  Event, which the Receipt keeps too, and in the `session` block of
  `history --json`, so the Platform can tie a Receipt to the Events it
  witnessed. A resumed session keeps its nonce; a Receipt written before
  this has none, and none is invented. The receipt-sync payload leaves it
  out until the Platform contract accepts the field.
- **Witness acknowledgement.** When the Platform acknowledges a batch it
  may return a witness acknowledgement. Core keeps the latest in the local
  spool state (`~/.openshard/remote-capture/`) and `openshard remote
  status` shows it:

  ```
  witness:   214 events acknowledged · last 12s ago
  witness:   not connected
  witness:   unknown (the Platform has not returned a witness acknowledgement)
  ```

  Only an acknowledgement the Platform actually returned counts. Events
  that were sent without one leave the witness unknown, never assumed.

Capture never waits for the witness: hooks append to the local spool and
return, sending happens off the hook path, and a failure there never fails
a hook or blocks the agent. Nothing new leaves the machine beyond the
nonce and, for Claude Code, `hook_config_hash` in the same first Event (a
hash of the installed OpenShard hook entries, never their headers): the
witness holds the same scrubbed, bounded Events remote capture already
sends (event types, tool names, repo-relative paths, statuses, counts,
hashes; no code, diffs, prompts, transcripts or output).

## Options considered

| Option | Strength | Cost | Use |
| --- | --- | --- | --- |
| (a) Per-install Ed25519 key, stored locally; public key registered with the Platform | Works offline, everywhere, no account. Weak against a same-user agent (threat 1). | One dependency; key handling. | Phase 1 |
| (b) Sigstore keyless: short-lived cert from an OIDC identity, logged in Rekor | Strong identity (e.g. "this GitHub workflow"), public log, no long-lived key. | Needs OIDC (CI, not laptops); network; heavy dependency; public log entries. | Phase 3, CI only, opt-in |
| (c) Platform counter-signature on ingest (server witness) | Independent party and independent time. Catches stripping and later rewrites. | Needs Platform work and key registration. | Phase 2 |

## Plan

- **Phase 1 (v0.5.0):** local Ed25519 key, a DSSE envelope carrying an
  in-toto Statement over the existing record hash, sealing at the points
  below, offline `openshard verify --signature`, and a bundle file a third
  party can check. No Platform change: signatures live in a sidecar file
  and are not sent, because the Platform contract is closed and rejects
  unknown keys.
- **Phase 2:** sync registers the install's public key and sends the
  envelope next to the Receipt; the hosted service checks it alongside its
  witness. Seal v2 brings this into v0.5.0 (receipt-seal-v2.md, "Rollout").
  The server counter-signature on ingest stays after v0.5.0.
- **Phase 3 (later):** optional Sigstore signing in CI
  (`pip install openshard[sigstore]`), producing a Sigstore bundle over the
  same Statement.

## Exact formats

### What is signed

Nothing new is canonicalised. The Statement names two digests:

- `record`: the record's existing `content_hash` (sha256 over
  `json.dumps(record minus content_hash, sort_keys=True,
  separators=(",", ":"), ensure_ascii=True, default=str)`), exactly as
  `compute_shard_hash` computes it over the bytes as stored.
- `projection`: sha256 over the same canonical JSON of the privacy-bounded
  `history --json` projection of that record at sealing time. Someone with only
  the projection can check it without seeing the full record.

Then DSSE signs the Statement bytes as they are: the signature input is
`PAE("application/vnd.in-toto+json", statement_bytes)` with
`PAE = "DSSEv1" SP len(type) SP type SP len(body) SP body`. Lengths are
decimal byte counts. No JSON canonicalisation is needed for the envelope
itself, so non-Python verifiers need only stock DSSE.

The record digest relies on Python's `json.dumps` rules, which a
non-Python verifier must reproduce. Seal v2 keeps these two digests as they
are and adds subjects digested with RFC 8785 (JCS), which TypeScript can
reproduce byte for byte (receipt-seal-v2.md).

### Statement (payload)

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {"name": "record:<receipt_id>", "digest": {"sha256": "<hex of content_hash>"}},
    {"name": "projection:<receipt_id>", "digest": {"sha256": "<hex>"}}
  ],
  "predicateType": "https://github.com/openshard/openshard/blob/main/docs/signing.md#seal-v1",
  "predicate": {
    "seal_version": "1",
    "receipt_id": "<receipt_id>",
    "shard_id": "<shard_id>",
    "sealed_at": "<UTC ISO-8601, local clock>",
    "seal_reason": "created | session_ended | idle_swept | turn_completed | amended",
    "previous_record_digest": "<sha256 hex of the previous seal, or null>",
    "core_version": "0.5.0",
    "covers": ["record_content"],
    "does_not_cover": ["agent_claims", "verification_outcome", "capture_completeness", "sealed_at"]
  }
}
```

`covers` / `does_not_cover` are the coverage statement. They are fixed by
`seal_version` so a verifier can print them, not interpret free text.

### Envelope

```json
{
  "payloadType": "application/vnd.in-toto+json",
  "payload": "<base64 Statement bytes>",
  "signatures": [{"keyid": "ed25519:<sha256 hex of the 32-byte raw public key>", "sig": "<base64>"}]
}
```

Envelopes are appended, one per line, to `.openshard/receipt-signatures.jsonl`.
`runs.jsonl` and the meaning of `content_hash` do not change.

### When a record is sealed

A hook-captured record changes at every `Stop`, so it is sealed when it
stops changing: at session end, or at the one-hour idle sweep (the same
points at which it becomes eligible for sync). One-shot records (`import`,
`wrap`, native runs) are sealed when written. A legitimate amendment
(`openshard note`, `feedback`, a `verify` attestation) writes a new seal
with `seal_reason: amended` and `previous_record_digest` set, so the chain
of seals shows every change. An `openshard verify` attestation lives in its
own file and is not part of today's `history --json` projection, so it adds
a seal only if it changes the projection; no seal covers the attestation
file itself. A record that was never sealed (written before signing shipped
or while it was off) is not sealed by a later amendment.

Sealing never blocks or fails a run. If it cannot sign, the record is
written anyway and a "not signed" line with a reason (`key_corrupt`,
`crypto_unavailable`, `content_hash_mismatch`, `seal_failed:<error>`, ...)
is appended to the sidecar; if the sidecar itself cannot be written, the
line goes to `~/.openshard/receipt-signing-failures.jsonl`. A record whose
stored `content_hash` already disagrees with its content is never sealed.
Sidecar writes are fsync'd appends under a cross-process lock on the sidecar
(the same lock mechanism the history uses), and each seal is decided from a
fresh read of `runs.jsonl` while that lock is held, so
the capture service and a CLI process can seal concurrently.

### Turning signing off

Signing is on by default. Any of these turns it off for new seals:

- `OPENSHARD_RECEIPT_SIGNING=off` (also `0`, `false`, `no`, `disabled`) in
  the environment of the process that writes the record, including the
  capture service;
- `receipt_signing: false` in the repository's `.openshard/config.yml`;
- `receipt_signing: false` in `~/.openshard/config.yml`.

An unreadable config never turns it off. When a Receipt that was sealed
earlier is amended while signing is off, a `signing_disabled` "not signed"
line is written, so it reads as unsigned rather than as changed after
sealing.

### Keys

- Private key: `~/.openshard/keys/receipt-signing.ed25519` (PKCS#8 PEM,
  unencrypted), file mode 0600, directory 0700, created on first seal.
  `OPENSHARD_HOME` applies. Never inside a repository, never logged, never
  synced. On Windows POSIX modes do not apply: the file is restricted with
  `icacls` (inheritance removed, current user only). This is best effort;
  if it cannot be confirmed the key is still used and `openshard doctor`
  warns ("the owner-only ACL could not be confirmed"). An existing key that cannot be read
  is never replaced; seals then record `key_corrupt` or `key_unreadable`.
- Public key: `receipt-signing.ed25519.pub` beside it (raw key, base64,
  plus keyid). `openshard keys show` prints it; `openshard keys export
  [-o FILE]` writes `{"algorithm", "keyid", "public_key"}` for someone else
  to pin with `openshard keys trust FILE` (stored in `keys/trusted/`). A
  pinned key must come from its owner, never from the bundle it checks.
- Rotation: `openshard keys rotate` writes a new key and moves the old
  public key to `keys/retired/<keyid>.pub` with `retired_at`. Old seals keep
  verifying against the retired key. Records are not re-signed.
- Revocation: `openshard keys revoke <keyid>` records `revoked_at`. Seals by
  that key dated after it are reported invalid; earlier ones verify but are
  labelled "key since revoked". Locally this rests on the self-asserted
  `sealed_at`; Phase 2 makes it trustworthy with the witness time. The
  install's current key cannot be revoked; rotate first, or every new seal
  would read invalid.

### Old and unsigned Receipts

Every Receipt written before this ships, and any written while signing is
off or failed, is **not signed**. It is shown as "Checksum only (not
signed)", never as failed. Its `content_hash` check still runs and keeps
its current meaning.

## `openshard verify --signature`

`openshard verify` already exists and re-runs checks. The signature check is
a mode of it that runs nothing, like `--ci`:

```
openshard verify --signature                      # latest Receipt
openshard verify --signature --receipt ID         # one Receipt
openshard verify --signature --file BUNDLE.json   # a bundle from anyone, offline
openshard verify --signature --receipt ID --bundle OUT.json [--include-record]
```

A bundle (`mediaType: application/vnd.openshard.receipt-bundle+json`,
`bundle_version: "1"`) holds the projection, the Receipt's sidecar lines
(seals and "not signed" notes) and the public keys of its seals; the full
record only with `--include-record`. The projection is sealed whole, so it
cannot be trimmed: it is the `history --json` row, which includes the task
text, file names, repository and branch. The CLI says so when it writes a
bundle. Public keys inside a bundle only prove the bundle is
self-consistent: a key whose keyid does not match its bytes is ignored, and
a key trusted here is always taken from here, never from the bundle. "Verified" needs
the key to be trusted: this install's own key, a key pinned with
`openshard keys trust <file>`, or (Phase 2) the Platform's registry.

Verdict rules v1 (`verdict_rules: "1"` in `--json`), first match wins:

| Verdict | When | Exit |
| --- | --- | --- |
| `invalid` | the signature does not verify, or the signed digest differs from the current record/projection, or the key was revoked before `sealed_at` | 1 |
| `not_signed` | no envelope for this Receipt | 0 (2 with `--strict`) |
| `signed_unknown_key` | signature verifies, key not trusted here | 0 (2 with `--strict`) |
| `verified` | signature verifies, digest matches, key trusted | 0 |

Every seal of the Receipt is checked, oldest first: each signature, and
that each seal links to the one before (`previous_record_digest`). Only the
latest seal is compared with the current record and projection; a broken
link or an earlier seal that does not verify is `invalid`. When the latest
sidecar line is a "not signed" note after earlier seals (signing was turned
off), the verdict is `not_signed` and says earlier seals exist. A seal whose
public key is not available here cannot be checked at all; it is reported
as `signed_unknown_key` with `signature_checked: false` and the words
"signed, not checked", never as verified. `--json` carries the verdict, the
reason, the per-seal chain, which digests were checked, and fixed lists of
what the signature binds (`bound_by_signature`), what is signed but only
self-asserted (`claimed_not_proven`) and what it never proves
(`not_proven`).

Output wording (one line per subject checked, then what it means):

```
Receipt    rcpt_… (this repository)
Signature  verified — sealed by ed25519:3f9a1c21… (this install) at 2026-10-17T14:02:00+00:00
Record     matches the sealed content (record digest checked)
Projection matches the sealed copy (projection digest checked)
Chain      2 seals, each linked to the one before (created, amended)
Means      the record can't be changed afterwards without this check failing, except by someone holding this key
Claimed    sealed_at, seal reason and Core version are signed but self-asserted (sealed_at is the sealing machine's clock)
Limit      a coding agent running as your OS user can read this key and seal a forged record; a copy shared earlier (a bundle, a CI artefact) is what catches a later rewrite
Not proven the agent's claims, the check results, or capture completeness
```

```
Signature  invalid — record changed after it was sealed (sealed 9c1e…, now 4b77…)
```

With only a bundle and no record, the `Record` line reads `full record not
provided; record digest not checked`. An unsigned Receipt reads
`Checksum only (not signed) — no seal for this receipt` (exit 0).

`openshard doctor` adds a "Receipt signing" block: whether signing is on,
the key's fingerprint (or that none exists yet), whether the key file is
owner-only (on Windows, whether the `icacls` ACL could be confirmed), and
any seals recorded in `~/.openshard/receipt-signing-failures.jsonl`.

## Receipt card "Tamper check" row

In Core today, `openshard last` adds one row after `Integrity`, checked
against this install's own key: `Signature  Signed · key <first 8 hex of
the keyid>`, `Checksum only (not signed)` (`Not signed` when the record has no valid
checksum), or `Invalid (<why>)`, e.g. `Invalid (record changed after it
was sealed)`.

The card's row is a Platform string; Core's `last --json` and each
`history --json` row carry a matching `signature_status`
(`verdict`, `reason`, `label`, `keyid`, `signature_checked`,
`verdict_rules`), from the same check `verify --signature` runs. It is
added outside the projection, so it never changes a sealed digest. Phase 2 wording:

| Row reads | When |
| --- | --- |
| Signed · verified | `verified` |
| Checksum only (not signed) | `not_signed`, content hash valid |
| Signature invalid | `invalid` |
| Signed · key not registered | `signed_unknown_key` (an edge case, so it is not shown as verified) |

A failed content hash keeps today's failure wording. A witnessed Receipt
shows the witness on its own line, so the install's claim and the server's
claim are never merged.

## Dependencies

The standard library has no Ed25519. `cryptography` is not a Core
dependency today. Choices: `cryptography` (PyCA, ~4.8 MB wheel plus `cffi`
~0.2 MB) or PyNaCl (~1.4 MB plus `cffi`). Recommendation: add
`cryptography>=42` to core dependencies: widely audited, abi3 wheels for
Linux, macOS and Windows, and `sigstore-python` already uses it, so Phase 3
adds no second crypto library. Verification needs it too, so an optional
extra would leave `verify --signature` unavailable by default.

## Privacy

An envelope holds digests, ids, timestamps, the keyid, the Core version and
the signature. No code, diffs, prompts, task text, paths or command output.
A bundle is different: even without `--include-record` it holds the
projection, which carries the task text, file names, repository and
branch, so share it only where the Receipt itself may go. Phase 3 Rekor entries are public (digest plus CI identity,
e.g. repository and workflow), which is why Sigstore is opt-in.

## What is open and what is hosted

Open, in Core, because it runs on the developer's machine or must be
inspectable to be trusted: local key generation, signing at seal time, the
envelope and Statement format on this page, and offline
`openshard verify --signature`. Anyone can read and rerun the verifier;
a verdict nobody can inspect would not be credible.

Hosted, provided by the Platform service: the witness of captured Events
(v0.5.0), the server counter-signature on ingest, the stored audit trail of
signed envelopes, the key registry and rotation UI, organisation key policies, signed audit exports,
and reconciling agent claims against independently observed evidence. Core
only sends the public key and envelopes to it; a hosted witness statement
uses the same open envelope format, so `openshard verify` can check it
offline. If Core's projection changed between sealing and sending (an
upgrade), sync re-seals first, so the projection digest matches what is
sent.

## Decisions

Approved for v0.5.0: the command is `openshard verify --signature`;
`cryptography>=42` is a core dependency; signing is on by default; seals
live in the sidecar `receipt-signatures.jsonl`, and only the seal made by
sync for the exact hosted receipt is sent (seal v2); the same-user-agent limit (threat 1) is stated publicly.
