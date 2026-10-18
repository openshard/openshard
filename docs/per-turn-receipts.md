# Per-turn Receipts (Claude Code first)

Status: first step shipped behind a flag, **off by default** for v0.5.0.
Turn it on with `OPENSHARD_TURN_RECEIPTS=on` in the capture service's
environment, or `turn_receipts: true` in `.openshard/config.yml`
(the environment wins; `off` there beats the config).

## Problem

Today one agent session is one Receipt: one `runs.jsonl` line, upserted at
every `Stop` and synced only after `SessionEnd` or an hour idle
(`sync/envelope.eligibility`). A chat kept open all day shows nothing on the
Platform until evening, and every task done in it is lumped into one Receipt.
Hosted Receipts are immutable, so the session Receipt cannot be sent early
and corrected later.

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
3. Not yet: same branch across sessions joins the Task; the PR links a
   branch's Receipts. When unsure, a new Task. See Still open.

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
| Independent re-runs | later | `openshard verify` / CI attach as evidence (`sync/evidence.py`), never rewrite |
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
turn stays "still in progress". Measured locally only in tests; the target is
"hosted within seconds".

## Platform contract

Nothing new is sent in this step, so the strict envelope
(`tests/fixtures/platform/receipt-sync-envelope.v1.json`) is unchanged:

* turn Receipts sync as ordinary Receipts; `session.session_key` relates the
  turns of one session, `session.start_source` is `turn`, `session.ended` is
  `false`;
* a `session`-grouped `task_id` is sent as `null`, because the contract
  defines `task_id` as an explicit declaration. An `explicit` one is sent.

For the Platform to show Tasks of turns, it must first accept (optional,
nullable): `task_grouping` (`explicit` | `session` | `branch`), and in
`session`: `receipt_scope`, `turn_index`, `previous_receipt_id`. Then Core can
send the session-grouped `task_id`. An "in progress" view is a separate
Platform PR; the hook for it is the in-progress turn Receipt already on disk
(`receipt_scope: turn`, no `turn_sealed_at`).

## Backwards compatibility

* Flag off: byte-for-byte today's record (a test asserts the shape).
* Older records render unchanged; nothing is back-filled.
* `openshard last` shows the latest turn; `history` lists every turn;
  `task attempts <task_id>` lists a session's turns; MCP `get_receipt` is
  unchanged.
* A session Receipt rebuilt from disk never turns into a turn Receipt
  mid-life when the flag flips; new sessions pick the flag up.

## Failure modes

| Case | Result |
|---|---|
| Turn never completes (crash, terminal closed, no `Stop`) | the open turn is swept after an hour idle exactly like a session today: `session_end_not_observed`, sealed `idle_swept`, synced |
| `SessionEnd` after the last sealed turn | ignored; no empty Receipt. The session end is not recorded on any turn (Still open) |
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
  starts with fresh offsets). Fine for normal chats; long ones should carry
  offsets forward (Still open).
* No new `runs.jsonl` rewrite: one append per turn and the existing upsert at
  the seal. The O(n) upsert itself is being fixed separately
  (`core-history-append`); this change adds no full-file rewrite.
* Sync wake reads `runs.jsonl` and the outbox once per seal.

## Other agents

* **Codex**: has `UserPromptSubmit`/`Stop`; usage is a cumulative counter, so
  a turn needs the existing `codex_usage_baseline` delta at each turn start.
* **Cursor**: `beforeSubmitPrompt`/`stop`; usage comes from the Cursor API by
  generation id, so a turn maps to one generation.
* **OpenCode**: `chat.message` / `session.idle` (idle is not a completed turn).
* **Antigravity, Hermes, Grok Build**: no reliable turn boundary yet.

`turn_scoped` returns false for every agent except Claude Code.

## Still open

1. Branch grouping across sessions and PR linking (`task_grouping: branch`).
2. Platform: accept the optional fields above; then send session-grouped `task_id`.
3. Record the session end as session-level evidence for the last turn.
4. Carry transcript offsets across turns.
5. Codex, Cursor and OpenCode turn boundaries.
6. Default the flag on once 1-3 land and a release has run with it.
