# Steering capture

Every time a person steers a coding agent, the Receipt says so: the first
prompt, each follow-up after a turn, a message typed while the agent was
still working, an Esc that stopped it, an answer to a permission prompt, and
files that changed between turns while the agent was not running. From those
facts each Receipt also carries a few derived **autonomy** facts (how often a
person stepped in, the longest stretch the agent worked alone).

Code: `openshard/adapters/steering.py` (capture), `openshard/adapters/steering_intent.py`
(the local classifier), `openshard/history/steering.py` (reading, autonomy,
sync projection, the `openshard last` line).

## Capability matrix

What each agent exposes, and where OpenShard reads it. "live hook": the agent's
own hook event, as it happens. "from log": the agent's own local session log,
read at each fold. "from git": the working tree. "not available": the agent
does not expose it; every Receipt of that agent lists the signal under
`not_available` with the reason, and it is never reported as zero.

| Agent | Initial prompt | Follow-up | Mid-turn interjection | Interrupt | Permission decision | Manual edit |
|---|---|---|---|---|---|---|
| Claude Code | live hook (`UserPromptSubmit`) | live hook (`UserPromptSubmit`) | from log (transcript `queue-operation` enqueue) | from log (transcript `[Request interrupted by user]`) | from log (transcript rejections; denials only) | from git |
| Codex | live hook (`UserPromptSubmit`) | live hook (`UserPromptSubmit`) | from log (rollout `user_message` inside an open task) | live hook (`Interrupt`; also rollout `turn_aborted`) | not available (1) | from git |
| Cursor | live hook (`beforeSubmitPrompt`) | live hook (`beforeSubmitPrompt`) | not available (2) | live hook (`stop` with `status: aborted`) | not available (3) | from git |
| OpenCode | live hook (`chat.message`) | live hook (`chat.message`) | not available (4) | not available (5) | not available (6) | from git |
| Google Antigravity | not available (7) | not available (7) | not available (8) | not available (9) | not available (10) | from git (between a `Stop` and the next model invocation) |
| Grok Build | live hook (`UserPromptSubmit`) | live hook (`UserPromptSubmit`) | not available (11) | not available (12) | not available (13) | from git |
| Hermes Agent | live hook (`pre_llm_call`) | live hook (`pre_llm_call`) | not available (14) | live hook (`on_session_end` with `interrupted`) | live hook (`post_approval_response`) | from git |

Why not:

1. Codex's documented hook events include no approval event, and approval
   requests are not read from the rollout log.
2. Cursor's hooks do not say whether a prompt was sent while a generation was
   running, and its transcript is deliberately not read.
3. Cursor shows permission decisions only to blocking `preToolUse` /
   `beforeShellExecution` hooks; OpenShard does not subscribe to them, because
   answering one is a decision.
4. The OpenShard OpenCode plugin forwards no signal that a message was sent
   while a turn was running.
5. An aborted OpenCode turn reaches the plugin only as `session.idle`, which
   also follows a normal turn end.
6. The OpenShard OpenCode plugin does not forward permission replies.
7. Antigravity delivers no user prompt to command hooks (`PreInvocation`
   fires per model call).
8. No prompt hook, and Antigravity's transcript has no published schema and
   is never read.
9. `Stop`'s `terminationReason` values are not documented, so a user stop
   cannot be told from other ends.
10. `PreToolUse` is a permission gate OpenShard does not subscribe to.
11. Grok Build's hooks do not mark messages sent mid-turn, and its session
    files are never read.
12. `StopCancelled` fires alike for a user interrupt, a declined permission
    and `--max-turns`.
13. `PermissionDenied` does not say whether a person, a rule or a policy denied.
14. Hermes' conversation history is never read, and no hook marks a message
    sent mid-turn.

Claude Code approvals are not available either: the transcript records a
person's rejection ("The user doesn't want to proceed with this tool use"),
never an approval, and Core installs no permission hook (it would decide).
Hermes "smart" approvals are a model's decision, not a person's, and are left
out.

**Cloud.** The same hooks fire inside a hosted agent VM (Claude Code on the
web, Cursor cloud agents) when Core is installed there, so the live-hook
signals are the same. The log signals need the agent's log inside that VM;
this has not been verified for hosted runtimes. A manual edit in a cloud VM
is only seen if a person changes that VM's working tree between turns.

## The steering event

Each fact is one entry in `capture.steering.events` (bounded to 100; the
overflow is counted in `events_dropped`). For the timeline, a hook-observed
prompt's existing `session.activity` Event gains `steering_kind` and `intent`;
log-observed interjections and git-observed manual edits add a
`session.activity` Event of their own.

| Field | Values |
|---|---|
| `at` | UTC time. Hook events: whole seconds; log events: the log's milliseconds |
| `turn_index` | the Receipt's turn (per-turn Receipts) or the session's turn count so far; absent when unknown |
| `kind` | `initial_prompt`, `follow_up`, `interjection`, `interrupt`, `permission_decision`, `manual_edit` |
| `intent` | follow-ups and interjections: `correction`, `redirect`, `clarification`, `approval`, `continue`, `new_task`, `unknown` |
| `intent_confidence`, `intent_rule` | `high` / `medium` / `low`, and the id of the rule that decided |
| `after` | `turn_end`, `failed_check` (the turn ended on a failed check), `tool_call`, `permission_prompt`, `none`, `unknown` |
| `source` | `hook`, `transcript` (the agent's own log), `git` |
| `prompt_chars` | the prompt's length in characters, never its text. A lower bound past 4,000 (the hook reader keeps at most 4,000 characters) and, for OpenCode, past 400 (its plugin truncates) |
| `think_seconds` | follow-ups: the pause since the turn ended, capped at 15 minutes (`think_capped`) |
| `from_queue` | a follow-up the agent took from its queue; already counted where it was typed |
| `decision` | permission decisions: `allow`, `deny`, `always_allow` |
| `files`, `lines_added`, `lines_removed`, `lines_unknown_files`, `paths` | manual edits (below) |

Rules that keep it honest:

* **Interjections are never inferred from hook order.** A prompt hook that
  arrives with no turn end observed since the last prompt is a follow-up with
  `after: unknown` (Claude Code fires no hook on Esc, so this is normal); if
  the log then shows the Esc, the follow-up becomes `after: turn_end` with
  its think time.
* **One Esc is one interrupt**: seen by the hook and again in the log, it is
  recorded once (the log's time wins). An Esc at a Claude Code permission
  prompt is the rejection, not a second input.
* **A queued message delivered as the next prompt** is counted once: as the
  interjection where it was typed. The prompt it became is `from_queue`.
* **A log that proves to be another session's** (a Codex rollout whose
  `session_meta` id differs) is never read again: `log_not_read_reason:
  log_session_mismatch`. A log over 256 MB is not read:
  `log_too_large`. Unknown log lines are skipped.

### Manual edits

At each turn end OpenShard records which paths were modified and their git
blob ids, plus HEAD (a small transient file under
`.openshard/claude_sessions/steering/`, named by a hash of the session). At the
next work in the session (the next prompt, or Antigravity's next model call) it
compares the tree again. Paths whose content changed in between are a
`manual_edit`: changed while no turn of this session was running.
OpenShard cannot tell a person from another tool (a formatter, another
agent), so `openshard last` says "changed between turns, not by the agent".
Paths another live agent session reported editing, and agent/OpenShard state
directories, are left out.

* A commit made by hand between turns is seen (HEAD moved).
* Lines are exact for a file that matched HEAD at the turn's end
  (`git diff --numstat`); a new untracked file counts its lines. A file the
  agent had already left modified has no earlier content OpenShard kept, so
  its lines are unknown (`lines_unknown_files`), never guessed; with no
  known file `lines_added`/`lines_removed` are `null`.
* No git, or HEAD unreadable on either side: no claim. A truncated snapshot
  or an uncomputable blob id marks the event `incomplete`.
* Paths (repo-relative, `sanitize_path`, at most 20) stay in the local
  record. They are never synced.

## Intent classifier (`rules-v1`)

Deterministic, local, explainable: an ordered list of English phrase rules in
`adapters/steering_intent.py`. No model, no network. Whole-message replies
first (`yes`, `lgtm`, `looks good` -> approval; `continue`, `go ahead` ->
continue), then a leading negation or correction phrase (`no`, `don't`,
`that's wrong`, `not what I asked`, `make it exactly like`, `redo`, `revert`,
`still failing`), redirects (`actually`, `instead`, `scratch that`, `let's go
with`), new work (`also`, `now do`, `one more thing`), questions ->
clarification (but "can you ...?" is a request, not a question), and a long
message with no reference back to the work -> `new_task` at low confidence.
Pasted content and code blocks are ignored. Anything else is `unknown`.

## Privacy

The prompt text is read only on the user's machine, to classify it, and is
dropped at once. Hook prompts are classified where the capture service reduces
the hook (`reduce_hook_payload`); the queue line carries only `prompt_facts`
(`intent`, `confidence`, `rule`, `chars`). Log messages (Claude Code queued
messages, Codex mid-turn messages) are classified at fold time and never
written anywhere. Nothing steering adds holds prompt text: not `runs.jsonl`,
not the Receipt, not the sync payload, not telemetry (steering adds no
telemetry property).

What was already true is unchanged: the prompt that opens a Receipt (the
session's first prompt, or each turn's with per-turn Receipts) is kept as the
Receipt's task, a secret-scrubbed excerpt of at most 300 characters
(`task`, `task_title`; `session.task_source: first_user_prompt_excerpt`).
That excerpt is the only place a follow-up's words can appear, and it
predates steering capture. Messages sent mid-turn never open a Receipt, so
their words appear nowhere.

`tests/test_steering_capture.py::TestPrivacy` feeds prompts holding a
secret-shaped key and distinctive phrases through Claude Code and Codex and
checks every file under `.openshard/`, the sync payload, the rendered Receipt
and the telemetry queue.

## Autonomy (derived)

`history/steering.py` derives these from the stored, sealed block; nothing
is computed from text.

| Fact | Meaning |
|---|---|
| `human_inputs`, `human_inputs_by_kind` | times a person stepped in; a kind the agent does not expose is `null` |
| `intents` | follow-ups and interjections by intent |
| `interrupts` | Esc / stop |
| `permission_prompts` | `asked` / `allowed` / `denied`; `null` where the agent does not expose it (Claude Code: denials only; Hermes: all three) |
| `longest_unsupervised_seconds` | the longest time the agent worked with no human input, over turns whose end was observed; `null` when none was |
| `steering_seconds` | sum of think time between a turn end and the next prompt, each pause capped at 15 minutes |

Local vs cloud and the permission mode are not repeated in the block: they are
the Receipt's existing `ran_on` (sync) / `capture.agent_host` and
`runtime_configuration.permission_mode`.

The block is part of the sealed `history --json` projection, which
`openshard verify --signature` recomputes, so the version 1 derivation is
frozen: changing it needs a new block version, or sealed Receipts would read
as changed after sealing.

`openshard last` prints one line, e.g.
`Steering  1 correction · 1 interrupt · 2 files changed between turns, not by the agent · you stepped in 4 times`;
a Receipt with only its first prompt says `no steering observed`. Receipts
captured before this feature have no block and print no line.

## Receipt, seal and sync

* Stored as `capture.steering` (version 1) on the record, so the seal covers
  it like every other capture fact; Receipts sealed before it have no block
  and verify unchanged (no field is back-filled).
* `history --json` and the sync envelope carry `steering`: the block without
  `paths` and `turn_ends`, at most 50 events, plus `autonomy`. It is optional
  in the receipt-sync contract (`tests/fixtures/platform/receipt-sync-envelope.v1.json`),
  proposed by Core; a Platform that does not know it rejects the key and Core
  resends the Receipt without it (`OPTIONAL_RECEIPT_KEYS`), keeping it locally.

## Limits

* Hook times are whole seconds; two facts within one second may be ordered
  by arrival rather than by the instant they happened.
* The Claude Code transcript and Codex rollout formats are not stable APIs.
  The shapes read here (`queue-operation`, `[Request interrupted by user]`,
  the rejection text, `event_msg` `task_started` / `user_message` /
  `turn_aborted`) match what Core already parses for usage and history
  import; an unknown shape under-reports, never invents.
* A Claude Code Esc fires no hook, so no turn-end snapshot is taken: manual
  edits right after an Esc are not seen.
