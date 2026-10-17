# Signed Receipts (design proposal)

Status: proposal, not implemented. Nothing on this page exists in Core yet.

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
| | When it was sealed. `signed_at` is the local clock, self-asserted, until a witness (Phase 2) adds its own time. |

Signing adds no evidence level. A signed `agent_reported` claim is still an
`agent_reported` claim. The independent re-run (`openshard verify`) and the
Platform witness (Phase 2) are separate claims and are shown separately.

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
- **Phase 2 (after v0.5.0):** `openshard sync connect` registers the
  install's public key and sync sends the envelope next to the Receipt.
  The hosted service checks it and adds its own witness signature (see
  "What is open and what is hosted").
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
non-Python verifier must reproduce; RFC 8785 (JCS) would be hash v2.

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
    "seal_reason": "created | session_ended | idle_swept | amended",
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
of seals shows every change. Sealing never blocks or fails a run: if it
cannot sign, the record is simply unsigned and `doctor` says why.

### Keys

- Private key: `~/.openshard/keys/receipt-signing.ed25519` (PKCS#8 PEM,
  unencrypted), file mode 0600, directory 0700, created on first seal.
  `OPENSHARD_HOME` applies. Never inside a repository. On Windows the file
  is created with an owner-only ACL; if that cannot be confirmed, `doctor`
  warns.
- Public key: `receipt-signing.ed25519.pub` beside it (raw key, base64,
  plus keyid). `openshard keys show` prints it for pinning out of band.
- Rotation: `openshard keys rotate` writes a new key and moves the old
  public key to `keys/retired/<keyid>.pub` with `retired_at`. Old seals keep
  verifying against the retired key. Records are not re-signed.
- Revocation: `openshard keys revoke <keyid>` records `revoked_at`. Seals by
  that key dated after it are reported invalid; earlier ones verify but are
  labelled "key since revoked". Locally this rests on the self-asserted
  `sealed_at`; Phase 2 makes it trustworthy with the witness time.

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

A bundle holds the projection, the envelopes and the public keys; the full
record only with `--include-record` (it can contain task text). Public keys
inside a bundle only prove the bundle is self-consistent. "Verified" needs
the key to be trusted: this install's own key, a key pinned with
`openshard keys trust <file>`, or (Phase 2) the Platform's registry.

Verdict rules v1 (`verdict_rules: "1"` in `--json`), first match wins:

| Verdict | When | Exit |
| --- | --- | --- |
| `invalid` | the signature does not verify, or the signed digest differs from the current record/projection, or the key was revoked before `sealed_at` | 1 |
| `not_signed` | no envelope for this Receipt | 0 (2 with `--strict`) |
| `signed_unknown_key` | signature verifies, key not trusted here | 0 (2 with `--strict`) |
| `verified` | signature verifies, digest matches, key trusted | 0 |

Output wording (one line per subject checked, then what it means):

```
Signature  verified — sealed by ed25519:3f9a…c21 (this install) at 2026-10-17T14:02Z
Record     matches the sealed content (record digest checked)
Means      unchanged since sealing by anyone without this key
Not proven the agent's claims, the check results, or capture completeness
```

```
Signature  invalid — record changed after it was sealed (sealed 9c1e…, now 4b77…)
```

With only a bundle and no record, the second line reads `Projection matches
the sealed copy (full record not provided; record digest not checked)`.

## Receipt card "Tamper check" row

The card's row is a Platform string; Core's `--json` gains a matching
`signature_status`. Phase 2 wording:

| Row reads | When |
| --- | --- |
| Signed · verified | `verified` |
| Checksum only (not signed) | `not_signed`, content hash valid |
| Signature invalid | `invalid` |
| Signed · key not registered | `signed_unknown_key` (an edge case, so it is not shown as verified) |

A failed content hash keeps today's failure wording. Once Phase 2 ships, a
witnessed Receipt adds a separate line, "Witnessed by Platform at <time>",
so the install's claim and the server's claim are never merged.

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
A bundle without `--include-record` holds only the already privacy-bounded
projection. Phase 3 Rekor entries are public (digest plus CI identity,
e.g. repository and workflow), which is why Sigstore is opt-in.

## What is open and what is hosted

Open, in Core, because it runs on the developer's machine or must be
inspectable to be trusted: local key generation, signing at seal time, the
envelope and Statement format on this page, and offline
`openshard verify --signature`. Anyone can read and rerun the verifier;
a verdict nobody can inspect would not be credible.

Hosted, provided by the Platform service: the server counter-signature
(witness) on ingest, the stored audit trail of signed envelopes, the key
registry and rotation UI, organisation key policies, signed audit exports,
and reconciling agent claims against independently observed evidence. Core
only sends the public key and envelopes to it; a hosted witness statement
uses the same open envelope format, so `openshard verify` can check it
offline. If Core's projection changed between sealing and sending (an
upgrade), sync re-seals first, so the projection digest matches what is
sent.

## Open questions

1. Is `openshard verify --signature` acceptable, or do you want a separate
   command? A bare `openshard verify <file>` would be confused with the
   existing check re-run.
2. Is adding `cryptography` (~5 MB) as a core dependency acceptable for
   v0.5.0?
3. Should signing be on by default in v0.5.0, or opt-in
   (`openshard keys init`) until Phase 2 gives it a visible payoff?
4. Is a sidecar file (`receipt-signatures.jsonl`) right, or should seals
   live inside the record (needs a Platform contract change before sync)?
5. Is the honest limit in threat 1 (a same-user agent can use the key)
   acceptable to state publicly at launch, or should Phase 2 ship first?
