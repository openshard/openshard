# Per-turn Receipts (Claude Code first)

On by default. Opt out with
`OPENSHARD_TURN_RECEIPTS=off` in the capture service's environment, or
`turn_receipts: false` in `.openshard/config.yml` (the environment wins).
A session already open when Core is upgraded keeps its session Receipt.

## Model

```
Task (task_id)           the job; may span prompts, sessions, agents, days
 └─ Receipt (receipt_id) one unit of work = one prompt -> completed turn
```

* **A turn** is `UserPromptSubmit` ... `Stop` of the main agent. Its Receipt
  is sealed when that `Stop` is applied and is final from then on.
* **No new architecture.** A turn Receipt is an ordinary hook Receipt. The
  next turn opens through the existing segment path
  (`claude_hooks._resumed_segment`, the one `claude --resume` already uses):
  new `run_id` / `receipt_id`, a working-tree baseline and HEAD taken at the
  turn's start, transcript usage counted from that start, and
  `capture.resumed_from_receipt_id` pointing at the previous turn. Signing,
  history, `openshard last`, MCP `get_receipt` and sync see one more Receipt,
  nothing else.
* **New local fields** (all under `capture`, absent when the flag is off):

| Field | Meaning |
|---|---|
| `receipt_scope: "turn"` | this Receipt covers one turn, not a session |
| `turn_index` | 1 for the session's first turn, +1 per turn |
| `turn_sealed_at` | when the turn's `Stop` was applied (final from then) |
| `status: "turn_completed"` | once sealed (was `in_progress`; `ended` stays for session ends) |
| `task_grouping` | `explicit` (declared `OPENSHARD_TASK_ID`) or `session` |
| `start_source: "turn"` | turn 2+ of a session whose previous turn completed |

## Grouping into Tasks

1. A declared task (`OPENSHARD_TASK_ID`, header-forwarded) wins:
   `task_grouping: explicit`. Unchanged binding rules (`_bind_task_context`).
2. Otherwise the first turn mints a `task_id` at its Receipt's creation and
   every later turn of the same agent session inherits it:
   `task_grouping: session`. This is a grouping OpenShard made from the
   session id it observed, never a declaration; that is why it is labelled.
3. A Task ends when the branch changes: a turn that starts on a different
   branch than the previous turn started on (the agent switched during that
   turn, or someone switched between turns) mints a new `task_id`. Unknown
   on one side counts as a change. A declared task is never split.
4. A Task also ends when its PR merges or closes. Once a turn of the Task
   found its PR (`gh`, at a `Stop`), the capture service asks GitHub for that
   PR's state at each later prompt (`gh pr view --json state`, read-only,
   5 s timeout); `MERGED` or `CLOSED` starts a new Task. No `gh`, no network,
   or inline hooks: not observed, and the Task carries on.
   The new Task's first Receipt records why (`capture.previous_task_ended`:
   `branch_changed`, `pr_merged` or `pr_closed`); the Task's PR is carried
   locally in `capture.task_pr_url`.

`history/task_identity.py` keeps its rule that `task_id` is never inferred from
prompts, timing or repository state; the session grouping is the one stated
exception and is always labelled.

## What is per turn

| Evidence | Per turn | How |
|---|---|---|
| Files changed | yes | git diff between the turn-start baseline and turn end; files already dirty at the turn start are `pre_existing` unless changed again |
| Commits | yes | `session_commits` from the turn-start HEAD to the turn-end HEAD, same corroboration rules |
| Usage / cost | yes | transcript messages timestamped at or after the turn start (`_transcript_usage`, `since = started_at`) |
| Checks | yes | check commands observed in that turn |
| Independent re-runs | yes | a turn with a resulting commit gets commit verification; with `post_session_verify: safe`, the working-tree run starts once the Task settles (see Settle check). Results attach as evidence, never rewrite |
| Session end | session-level | no turn Receipt claims it; see Failure modes |

## Sealing and signing

`close_turn` marks `turn_sealed_at`, the fold runs with `finalize=True` (git
outcome for the turn) and the staging buffer is dropped.
`receipt_signing.seal_point` returns `turn_completed` for a sealed turn, so the
write that seals the turn also signs it (`seal_reason: turn_completed`).
Amendments chain re-seals exactly as today. The parity test keeps
`seal_point` and sync eligibility equal.

## Sync

`envelope.eligibility` returns `turn_sealed` for a sealed turn Receipt, even
while the session is open. In the capture service the seal calls
`sync.client.request_sync()`, which wakes the background sync loop at once
(polling at 250 ms) instead of waiting for its 5-minute tick. The in-progress
turn stays "still in progress".

## Settle check

With `post_session_verify: safe`, a sealed turn that changed files no longer
starts `openshard verify` at its `Stop`. It is noted as its session's pending
re-run (`.openshard/verify-requests/settle-*.json`), and the run starts when
the Task settles: no new prompt for `post_session_verify_settle_minutes`
(default 10) after the session's latest sealed turn, or the session's next
sealed turn is in a new Task. A turn with a commit is verified on commit, as
before, and clears the pending run. One run per pending Receipt, and none
for a clean HEAD that already has a bound re-run. Checked by the capture
service's sync timer (every 5 minutes, linked or not) and at any later seal
in the repository, so a run starts 10 to 15 minutes after the last turn.

## Platform contract

The Platform accepts the turn fields (all optional, nullable). For a turn
Receipt Core sends:

* `task_id` with `task_grouping`: `session` for a Task OpenShard grouped from
  the observed session, `explicit` for a declared one. Absent means explicit,
  so session Receipts are unchanged;
* in `session`: `receipt_scope: "turn"`, `turn_index` and
  `previous_receipt_id` (the previous turn's Receipt), beside the existing
  `session_key`, `start_source: "turn"` and `ended: false`.

The same `session` fields are in `openshard history --json`; MCP
`get_receipt` adds a `turn` object (`label`, `turn_index`, `session_key`,
`previous_receipt_id`) for turn Receipts only. `openshard last` and
`openshard history` print `Turn 2 of session ses_1a2b3c4d`.

## In progress

A sealed turn syncs; an open one does not (hosted Receipts are immutable). The
open turn is signalled on the existing remote / connected capture journal
instead (`openshard/remote`), so only for a linked session:

* the `UserPromptSubmit` Event (`session.activity`) that opens a turn carries
  `turn_opened: true`, `receipt_scope: "turn"`, `turn_index` and the open
  turn's `receipt_id`;
* the `Stop` that seals it adds a `receipt.sealed` Event (action
  `turn completed; Receipt final`, `turn_status: "turn_completed"`) with the
  same three facts. Final is not signed: a signature depends on the install key.

For the Platform: a turn is "in progress" while its journal has a
`turn_opened` Event for a `receipt_id` and neither a `receipt.sealed` Event for
it nor a synced Receipt with that id. A capture that goes silent
(`silentAfterSeconds`) is "interrupted", never "in progress". An unlinked
session sends nothing until its turn seals.

## Backwards compatibility

* Opted out: byte-for-byte today's record (a test asserts the shape).
* Older records render unchanged; nothing is back-filled or rewritten.
* `openshard last` shows the latest turn; `history` lists every turn;
  `task attempts <task_id>` lists a session's turns; MCP `get_receipt` adds
  `turn` for turn Receipts only.
* A session Receipt never turns into a turn Receipt mid-life: not when rebuilt
  from disk, and not when a buffer an older Core opened (more than one prompt
  or turn already) meets the new default. New sessions pick the flag up.
* Files a previous turn left uncommitted are listed as pre-existing
  ("changed before the turn") and are not counted as the turn's changes.

## Failure modes

| Case | Result |
|---|---|
| Turn never completes (crash, terminal closed, no `Stop`) | the open turn is swept after an hour idle exactly like a session today: `session_end_not_observed`, sealed `idle_swept`, synced |
| `SessionEnd` after the last sealed turn | ignored; no empty Receipt, and nothing streamed to remote capture. The session end is not recorded on any turn |
| A late or repeated `Stop` after a sealed turn | ignored (it can carry no work); no empty buffer is left behind |
| `claude --resume` after the session ended | the next Receipt says `start_source: resume`, not `turn`, and stays in the session's Task |
| Another Stop hook blocks and the agent continues | Claude's hooks are synchronous and ordered, so the next hook opens the next turn Receipt (no prompt; task text not captured) instead of being dropped |
| Capture service down (inline hooks) | same sealing in the hook process; no PR lookup; no immediate sync (the next sync run picks it up) |
| Hosted runtimes (`claude-code-web`, `cursor-cloud`) | unchanged: Stop seals with `session_end_not_observed` (logic moved into `claude_turn_receipts.close_turn`) |

## Privacy

Unchanged: paths and counts only, never prompts, diffs, transcript content or
command output. The new fields are an enum, an integer, a timestamp and an id.

## Performance

* Opening a turn costs one `_new_buffer` (git branch/HEAD/dirty and a bounded
  baseline snapshot) in the capture worker, never on the hook's blocking path.
* The transcript is re-read from the start for each turn (the per-turn reader
  starts with fresh offsets). Fine for normal chats.
* No new `runs.jsonl` rewrite: one append per turn and the existing upsert at
  the seal.
* Sync wake reads `runs.jsonl` and the outbox once per seal.

## Other agents

Per-turn Receipts apply to Claude Code only (`turn_scoped` returns false for
every other agent). Other agents keep one Receipt per session.
