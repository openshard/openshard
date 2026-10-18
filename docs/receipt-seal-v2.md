# Receipt seal v2: checking the hosted copy

Status: v0.5.0. Core seals new Receipts with v2 (this page) and sync sends
the seal and registers the key; v1 seals keep verifying. The Platform's
check ships separately, in the order under "Rollout".

Read [signing.md](signing.md) first. Everything there still holds: a seal
is one narrow claim, *the holder of key K sealed this content*, it adds no
evidence level, and **a coding agent running as your OS user can read the
key and seal a forged record**. Seal v2 does not change that limit. The
public promise stays "can't be changed afterwards".

## The problem

A v1 seal covers the local record and its `history --json` projection.
The Platform receives a different object: the privacy-bounded receipt in
the sync envelope (`sync/envelope.py: receipt_payload`). It differs from the
projection (the `multi_agent` block depends on an organisation setting;
sync adds `session.task_source`), and it carries no record. So nobody could
check that the copy on the Platform is the one this machine sealed.

Seal v2 adds two things to the Statement:

1. a digest of the exact receipt sync sends, so the Platform and anyone
   reading the hosted copy can check it against the install's public key;
2. salted commitments to every field of the stored record, so the owner
   can later prove what one withheld field said by revealing it and its
   salt, without revealing anything else.

## Statement

Same DSSE envelope and in-toto Statement as v1 (signing.md, "Exact
formats"). The signed bytes are still the Statement as serialised, so DSSE
needs no canonical form. What changes:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [
    {"name": "record:<receipt_id>",        "digest": {"sha256": "<as v1>"}},
    {"name": "projection:<receipt_id>",    "digest": {"sha256": "<as v1>"}},
    {"name": "record_fields:<receipt_id>", "digest": {"sha256": "<JCS digest of the commitment map>"}},
    {"name": "sync:<receipt_id>",          "digest": {"sha256": "<JCS digest of the synced receipt>"}}
  ],
  "predicateType": "https://github.com/openshard/openshard/blob/main/docs/receipt-seal-v2.md#seal-v2",
  "predicate": {
    "seal_version": "2",
    "receipt_id": "...", "shard_id": "...", "sealed_at": "...", "core_version": "...",
    "seal_reason": "created | session_ended | idle_swept | turn_completed | amended | synced",
    "previous_record_digest": "<sha256 of the previous seal's Statement bytes, or null>",
    "canonicalization": "https://www.rfc-editor.org/rfc/rfc8785",
    "uncommitted_fields": [],
    "covers": ["record_content", "record_fields", "synced_receipt"],
    "does_not_cover": ["agent_claims", "verification_outcome", "capture_completeness", "sealed_at"]
  }
}
```

- `record:` and `projection:` are digested exactly as in v1, so local
  checks are unchanged.
- `record_fields:` is on every v2 seal. A v2 Statement without it is
  `invalid` (`malformed_statement`).
- `sync:` is only on a seal made by sync (`seal_reason: synced`).
  `synced_receipt` in `covers` is bound only by a seal that names it.
- `uncommitted_fields` names any field with no JSON form (NaN, an unpaired
  surrogate). It is listed, never dropped silently.

Verification is backward compatible: `predicateType` selects the version
(v1 type with `seal_version: "1"`, v2 type with `"2"`); anything else is
`invalid` (`unknown_statement_type` / `unknown_seal_version`). A chain may
mix v1 and v2 seals; links work as in v1. Verdict rules stay version 1.

## Canonical JSON: RFC 8785 (JCS)

The two new digests are sha256 over the RFC 8785 bytes: UTF-8, object
keys sorted by UTF-16 code units, numbers in ECMAScript `Number#toString`
form, strings escaped as `JSON.stringify` does. Core implements it in
`openshard/util/jcs.py`; the Platform implements the same in TypeScript.

Why not the repo's existing canonical JSON: Core's `_canonical_json` uses
Python's `ensure_ascii` escaping and float `repr`, which TypeScript cannot
reproduce cheaply. And the Platform's existing `canonicalJson`
(`JSON.stringify` over re-sorted objects) is not JCS either: JavaScript
always puts integer-like keys (`"1"`, `"10"`) first. Both keep their
current uses; only seal v2 digests use JCS.

`tests/fixtures/seal-v2/vectors.json` holds input JSON, the expected
canonical bytes and sha256 (including the RFC's own examples and
integer-like keys), plus a complete hosted seal made with a fixed test key.
Core and the Platform both test against this one file.

## What the sync subject covers

`sync:` is the JCS digest of the `receipt` object of the receipt-sync
envelope, as it goes on the wire (serialised the way sync serialises it,
then parsed back): `receipt_seal_v2.sync_digest`. Not the `source` block,
not the envelope. The Platform stores that object as received, so it can
recompute the digest from storage at any time.

Sync seals at send time (`receipt_signing.seal_for_sync`), because only
sync knows the exact receipt (the `multi_agent` block depends on the
organisation). Under the sidecar lock it re-reads `runs.jsonl` and:

- signing off, never sealed, or the latest sidecar line is a "not signed"
  note: no seal, the Receipt is sent unsigned. Sync never seals a record
  after the fact;
- the stored `content_hash` differs from the one sync built the receipt
  from: no seal (it changed in between; the next flush tries again);
- the latest seal already names this record, projection and receipt: it is
  reused (a retried send does not grow the chain);
- otherwise a new seal with `seal_reason: synced`, chained to the previous
  one. Its `record:` digest is today's, so the chain shows the hosted copy
  came from the same sealed record.

## Field commitments

For every top-level field of the stored record except `content_hash`:

```
commitment = sha256(JCS({"field": name, "receipt_id": id, "salt": salt, "value": value}))
record_fields digest = sha256(JCS({name: commitment, ...}))
```

`salt` is 32 random bytes (base64url, no padding), fresh for every field
of every seal, so two seals of the same value do not match and a reader of
the digests learns nothing about the values. Every field is committed, not
only the withheld ones, because "withheld" depends on the projection
version; committing all of them means any field can be revealed later.

The salts and the commitment map live in `.openshard/receipt-seal-salts.jsonl`,
beside `runs.jsonl` and the sidecar, one line per seal keyed by the sha256
of its Statement bytes. The line is written before the envelope, under the
same lock, so no v2 seal exists without its salts. Sync code never reads
this file (a test checks), and a bundle carries a salt only for a field the
owner reveals. Anyone who can read the salts file can also read
`runs.jsonl`, so it exposes nothing that was not already there; it does let
them make reveals, so share it only where the record may go.

### Reveal

```
openshard verify --signature --receipt ID --bundle OUT.json --reveal task [--reveal files]
openshard verify --signature --file OUT.json
```

A reveal is `{"_type": ".../receipt-seal-v2.md#reveal-v1", "receipt_id",
"statement_sha256", "field", "value", "salt", "commitments"}`. A verifier
checks that it names the bundle's latest seal, that the map digests to the
signed `record_fields:` subject, and that `value` and `salt` give the
committed digest. The map holds only digests, so it reveals field names and
nothing else. A reveal that does not match makes the bundle `invalid`
(`reveal_mismatch`); a reveal is never checked against a seal whose
signature was not. Core before v2 ignores reveals.

## Sending the seal and registering the key

The sync envelope gains one optional top-level key beside `receipt`:

```json
"seal": {
  "envelope":   {"payloadType": "application/vnd.in-toto+json", "payload": "...", "signatures": [{"keyid": "...", "sig": "..."}]},
  "public_key": {"algorithm": "ed25519", "keyid": "ed25519:<sha256 hex>", "public_key": "<base64 raw 32 bytes>"}
}
```

It is outside `receipt`, so the receipt, its `payload_hash` and the
Platform's idempotency are unchanged; a Receipt without a seal is sent
exactly as before. The Platform contract is closed, so the Platform must
accept `seal` before Core sends it.

The public key travels with the seal so the Platform can check the
signature even before the key is registered, but a carried key is never
trusted (as with bundles). Trust comes from registration:
`POST /v1/orgs/:orgId/signing-keys` with `{"algorithm", "keyid",
"public_key"}`, idempotent, authenticated like sync. Core registers the
current key during a sync flush that sends a seal. A Platform without the
route answers 404 and Core sends the seal anyway.

What registration proves, plainly: a member or API key of the organisation
said this key is theirs, at the Platform's time. Anyone holding the sync
credential can register a key. It does not prove which machine holds it.

## Verdicts on the Platform

The Platform checks the stored seal on read against the stored receipt,
with the names Core uses (signing.md, verdict rules v1), first match wins:

| Verdict | When | Card reads |
| --- | --- | --- |
| `not_signed` | no seal was sent | Not signed |
| `invalid` | malformed, wrong statement type or receipt id, signature does not verify, or the `sync:` digest differs from the stored receipt | Signature invalid |
| `signed_unknown_key` | everything checks with the carried key, but that key is not registered to this organisation | Signed · key not registered |
| `verified` | everything checks with a key registered to this organisation | Signed · registered key (in full: Signed · matches this organisation's registered key) |

`verified` means: the hosted receipt is byte for byte what the holder of a
key registered to this organisation sealed. It does not mean the work was
right, the checks passed, capture was complete, or that the agent did not
forge it with the same user's key. Registration is trust-on-first-use, so
the card says the seal matches the organisation's registered key, never
that Openshard verified the Receipt. The card keeps the content-hash
`Checksum` row and the witness row separate from this one.

## Rollout

1. Core: seal v2 locally (this page, reveals, vectors). No wire change.
2. Platform: the contract accepts `seal` (optional). Deploy.
3. Platform: store `seal`, key registration, verdict on read, card row.
4. Core: sync sends `seal` and registers the key; the Core copy of the
   strict Platform schema is updated in the same change.

Step 4 must not ship before step 2 is deployed: an older Platform rejects
the unknown key. Core then resends the receipt without the seal, so the
Receipt still syncs, but unsigned.

## Not in v2

Platform-side reveal checks, key revocation on the Platform, flagging
unsigned Receipts from an install whose key is registered (signing.md
threat 3), and a Platform counter-signature are later work.
