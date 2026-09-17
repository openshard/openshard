# Platform sync (hosted Receipt history)

`openshard sync` sends copies of this repository's Receipts to an OpenShard
Platform organisation so they can be read from a web dashboard. This page
is the complete contract: what leaves the machine, when, how failures are
handled, and what is deliberately not done yet.

The short version:

- The canonical Receipt stays in `.openshard/runs.jsonl`. The Platform
  holds a **copy**; it never edits, enriches or re-derives a Receipt.
- What is sent is exactly the privacy-bounded machine receipt that
  `openshard history --json` prints (`history/views.py`): no prompts,
  transcripts, diffs, command output, agent notes, environment values or
  absolute paths. Hook-captured Receipts carry no folder name either; the
  repository is named by its canonical `repo_identity` (`host/owner/repo`).
- Nothing is sent until `openshard sync connect` has stored a link, and
  `OPENSHARD_PLATFORM_SYNC=off` or `platform: {sync: false}` in a
  repository's `.openshard/config.yml` stops it at any time.
- Sending is idempotent and retry-safe. `openshard sync now` can be run
  as often as you like; the capture service runs it in the background.

## Commands

| Command | What it does |
| --- | --- |
| `openshard sync connect --endpoint URL --org UUID [--api-key osk_...]` | Stores the link in `~/.openshard/platform.json` (mode 0600; `OPENSHARD_HOME` applies). Prompts for the key without echo when omitted. Never writes into a repository. |
| `openshard sync status [--json]` | Where Receipts go, and for this repository: synced, pending, still-in-progress, changed-locally, conflict and rejected counts. The key is shown as its public prefix only. |
| `openshard sync now [--limit N] [--json]` | Sends this repository's eligible unsynced Receipts. |
| `openshard sync disconnect` | Forgets the link. Hosted copies are not deleted. |

Environment overrides for CI and scripts: `OPENSHARD_PLATFORM_ENDPOINT`,
`OPENSHARD_PLATFORM_ORG_ID` and `OPENSHARD_PLATFORM_API_KEY`. All three
must be set for the override to apply; a partial set is ignored rather
than mixed with the stored file.

## What crosses the wire

One HTTPS `POST` per Receipt to `<endpoint>/v1/orgs/<org>/receipts` with
`Authorization: Bearer osk_...`, carrying the receipt sync envelope v1:

```json
{
  "contract": "openshard.receipt-sync",
  "contract_version": "1",
  "source": { "product": "openshard-core", "version": "0.4.5", "receipt_schema_version": "1.2" },
  "receipt": { "...": "openshard history --json projection of one record" }
}
```

`source.receipt_schema_version` is the record's own stamped version
(`unknown` for records that predate stamping), never today's. The
Platform's contract is closed: it rejects any key it does not define, so a
new Core field becomes hosted data only after the contract is updated
there. Plain `http://` is refused except to loopback.

The Platform identifies a hosted Receipt by `(organisation, receipt_id)`.
Records written before v0.4.4 have no `receipt_id` and cannot sync;
`status` counts them under "cannot sync".

## When a Receipt is sent

A record written once (`import`, `wrap`, a native run) is eligible as soon
as it exists. A hook-captured session is upserted into `runs.jsonl` at
every `Stop`, so its record keeps changing while the agent session is
open. It is sent only when:

- the session ended (`capture.session_end_observed`), or
- it has been idle for an hour (the same threshold after which capture
  itself sweeps a stale session).

Until then `status` reports it as "still in progress".

## How the Platform answers, and what happens next

| Answer | Meaning | Local state |
| --- | --- | --- |
| 201 created | stored | `synced` |
| 200 duplicate | already stored with the same content (a replay) | `synced` |
| 409 conflict | different content already stored under this `receipt_id` | `conflict`; never retried |
| 400 / 413 / 422 | payload refused (schema, size, privacy) | `rejected` with the error code and field paths; never retried |
| 401 / 403 / 404 | the key, organisation or endpoint is wrong | link paused for an hour; `connect` clears it |
| 429 / 5xx / unreachable | try later | flush stops; exponential backoff (1 min to 1 h) |

Local state lives in `.openshard/sync-outbox.jsonl`, one line per
`receipt_id` that reached a decision. Pending work is not stored: it is
derived on every run by comparing `runs.jsonl` with that file, so a
crash mid-flush can neither lose nor duplicate anything. The file holds no
secret and no payload. A record synced to one organisation is sent again
when you connect to a different one.

## Changed locally after sync

`openshard note` and `openshard feedback` amend a stored record, and a
hook session can occasionally fold again after it was sent. The stored
`content_hash` moves when that happens, and `status` reports the Receipt
as "changed locally": the hosted copy is the earlier one. Nothing is
resent in this version, because the Platform keeps the first copy it
accepted and treats different content under the same `receipt_id` as a
conflict. Hosted receipt revisions are the next Platform step.

## Not done, on purpose

- **Task grouping.** `task_id` (`task_` + UUIDv7, minted only by
  `openshard task new` and attached at record creation with `--task-id`)
  travels in the receipt exactly as stored, and is `null` for every
  Receipt that never declared one. The Platform stores and filters on it
  and never mints, infers or reconstructs one. Task views are a later
  step.
- **Prompt-derived task text.** `task_short` / `task_full` are scrubbed,
  bounded excerpts of the first prompt for hook-captured sessions, and
  are sent as Core exports them. Stripping them on ingest is a planned
  organisation-level Platform option.
- **Signatures.** `integrity` is Core's tamper-evidence verdict for the
  local record. The Platform stores the string and shows it. It is not a
  signature and proves nothing about who ran the session.
