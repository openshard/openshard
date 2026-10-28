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
own hook event (or OpenShard's own plugin), as it happens. "from log": the
agent's own local session log, read on the machine where the agent runs, at
each fold (`adapters/steering.py`, `adapters/steering_logs.py`). "from git":
the working tree. "not available": the agent itself does not record it; every
Receipt of that agent lists the signal under `not_available` with the reason,
and it is never reported as zero.

| Agent | Initial prompt | Follow-up | Mid-turn interjection | Interrupt | Permission decision | Manual edit |
|---|---|---|---|---|---|---|
| Claude Code | live hook (`UserPromptSubmit`) | live hook (`UserPromptSubmit`) | from log (transcript `queue-operation` enqueue) | from log (transcript `[Request interrupted by user]`) | live hook (`Notification` `permission_prompt`, then that tool ran: allow, inferred) + from log (transcript rejections: deny) | from git |
| Codex | live hook (`UserPromptSubmit`) | live hook (`UserPromptSubmit`) | from log (rollout `user_message` inside an open task) | live hook (`Interrupt`; also rollout `turn_aborted`) | from log (rollout `exec command / patch rejected by user`: deny only) | from git |
| Cursor | live hook (`beforeSubmitPrompt`) | live hook (`beforeSubmitPrompt`) | from log (agent transcript: no `turn_ended` and no `stop` since the previous prompt) | live hook (`stop` with `status: aborted`) | from log (`state.vscdb` `toolFormerData.userDecision`: allow / deny) | from git |
| OpenCode | live hook (`chat.message`) | live hook (`chat.message`) | live hook (`chat.message` while `session.status` is busy; plugin v6) | live hook (`session.error` `MessageAbortedError`; plugin v6) | live hook (`permission.asked` / `permission.replied`; plugin v6) | from git |
| Google Antigravity | from log (transcript `USER_INPUT` step) | from log (`USER_INPUT` after a `Stop`) | from log (`USER_INPUT` with no turn end since the previous prompt) | live hook (`Stop` with `terminationReason: USER_CANCELED`) | not available (1) | from git (between a `Stop` and the next model invocation) |
| Grok Build | live hook (`UserPromptSubmit`) | live hook (`UserPromptSubmit`) | from log (`updates.jsonl`: user message after agent output, before `turn_completed`) | live hook (`StopCancelled` `reason: user_interrupt`, `cancelledBy: user`) | live hook (`StopCancelled` `permission_rejected` / `permission_cancelled` by `user`: deny) + from log (`updates.jsonl` ACP option kind, where recorded) | from git |
| Hermes Agent | live hook (`pre_llm_call`) | live hook (`pre_llm_call`) | from log (`state.db` `display_kind: steer`, or `_queued_prompt`) | live hook (`on_session_end` with `interrupted`) | live hook (`post_approval_response`) | from git |

Why not:

1. Antigravity records no approval decision: its transcript writes a step
   waiting for approval only once it is answered, with no decision field, and
   its only permission hook (`PreToolUse`) is a gate that must answer the
   prompt, which OpenShard never does.

What each count can say (`autonomy.permission_prompts`, `null` = the agent
does not record it): Claude Code asked / allowed / denied; Codex denied only
(Codex persists neither an approval nor an approval request: approvals are
`Op`s, never rollout lines); Cursor allowed / denied (no record of a prompt
shown); OpenCode and Hermes all three; Grok Build denied (its hooks name no
approval, and its log's permission lines are read only where present).
Hermes "smart" approvals are a model's decision, not a person's, and are left
out.

How the new sources are read, and their limits:

* **Claude Code approvals** are inferred, never observed: Claude Code fires no
  hook with a person's answer. Core installs a `Notification` hook (matcher
  `permission_prompt`), which by Claude Code's design cannot answer or block
  the prompt; the tool it names (`"... permission to use Bash"`) running next
  (`PostToolUse`) is recorded as `allow`. A failed call, another tool, or the
  turn ending first clears the prompt with no claim. The VS Code extension
  fires no such notification, and a notification Claude Code sends late or
  for no visible dialog (known Claude Code issues) can misattribute one.
  Core never installs `PermissionRequest` (it sits in the decision path).
* **Cursor**: the transcript lines carry no time, so a transcript row never
  stands alone: the newest rows are paired with this Receipt's prompt hooks
  (newest first), and only a prompt the hook saw with no turn end observed,
  whose row also has no `turn_ended` before it, becomes an interjection.
  Decisions come from Cursor's local store, opened read-only; a row without
  a time is skipped; the conversation id is assumed to be the store's
  composer id (observed by third-party readers, not documented).
* **Antigravity**: a `Stop` within one second of a prompt step (hooks carry
  whole seconds) leaves the prompt's `after` unknown. `USER_CANCELED` is
  reported in real payloads; some CLI builds fire no `Stop` on a cancel at
  all, so an interrupt can still go unseen.
* **Grok Build**: Esc never cancels a turn (Grok's own docs); the interrupt
  is Ctrl+C, the stop button or the dashboard. A runtime cancel (`max_turns`,
  `no_progress`) is never a person's. The log's line format is not
  documented beyond "one ACP session update per line"; only the shapes named
  above are read.
* **Hermes**: Hermes' default busy mode (`interrupt`) ends the running turn
  and makes the message the next prompt: an interrupt plus a follow-up, as
  observed. Only `steer` and `queue` modes leave an interjection.

## Every environment

The same agent exposes different signals depending on where it runs. Each
row is an (agent, environment) pair OpenShard has a capture path for; a pair
that is not listed is not claimed. "from GitHub": derived from the pull
request (below). A test keeps this table equal to
`adapters/steering.py: ENVIRONMENT_ROWS` / `environment_capability`.

| Agent | Environment | Initial prompt | Follow-up | Mid-turn interjection | Interrupt | Permission decision | Manual edit |
|---|---|---|---|---|---|---|---|
| Claude Code | `local_terminal` | live hook | live hook | from log | from log | live hook + from log | from git |
| Claude Code | `local_ide` | live hook | live hook | from log | from log | live hook + from log (K) | from git |
| Claude Code | `cloud_workspace` | live hook | live hook | from log | from log | live hook + from log | from git |
| Claude Code | `cloud_agent` | live hook | live hook | from log (A) | from log (A) | live hook + from log (A) | from git (B) |
| Claude Code | `ci` | live hook | not available (C) | not available (C) | not available (C) | not available (C) | not available (C) |
| Codex | `local_terminal` | live hook | live hook | from log | live hook | from log | from git |
| Codex | `cloud_workspace` | live hook | live hook | from log | live hook | from log | from git |
| Codex | `cloud_agent` (D) | live hook | live hook | from log (A) | live hook | from log (A) | from git (B) |
| Codex | `ci` | live hook | not available (C) | not available (C) | not available (C) | not available (C) | not available (C) |
| Cursor | `local_ide` | live hook | live hook | from log | live hook | from log | from git |
| Cursor | `cloud_agent` | live hook | live hook | from log (A) | live hook | from log (A) | from git (B) |
| OpenCode | `local_terminal` | live hook | live hook | live hook | live hook | live hook | from git |
| OpenCode | `cloud_workspace` | live hook | live hook | live hook | live hook | live hook | from git |
| Google Antigravity | `local_ide` | from log | from log | from log | live hook | not available (1) | from git |
| Grok Build | `local_terminal` | live hook | live hook | from log | live hook | live hook + from log | from git |
| Grok Build | `cloud_workspace` | live hook | live hook | from log | live hook | live hook + from log | from git |
| Hermes Agent | `local_terminal` | live hook | live hook | from log | live hook | live hook | from git |
| Hermes Agent | `cloud_workspace` | live hook | live hook | from log | live hook | live hook | from git |
| Grok Bot | `cloud_agent` | not available (E) | not available (E) | not available (E) | not available (E) | not available (E) | not available (E) |
| Any PR-driven agent | `web` | not available (F) | from GitHub | not available (G) | not available (H) | not available (I) | not available (J) |

Why, beyond the numbered reasons above:

* (A) Read inside the hosted VM, where the agent writes its log, by the same
  code as locally; tested with fixture transcripts, not yet on a live hosted run.
* (B) The hosted VM's working tree between turns. A person seldom has a shell
  there, so a change is usually another process's; it is still "not the agent".
* (C) A CI run is headless: no person is there to type, stop, answer or edit
  while it runs. The run's prompt is the only human input.
* (D) Only where the Codex cloud environment runs trusted command hooks.
  Cloud-orchestrated ChatGPT Work runs none, so nothing is captured there
  in-run; only the `web` row applies once it opens a pull request.
* (E) Grok Bot runs no hooks; Action Recording exports actions, not the
  conversation, and its self-report carries no steering.
* (F) The task reaches a PR-driven agent through its own app, or an issue or
  chat OpenShard does not read; GitHub shows only what follows on the PR.
* (G) GitHub does not show whether the agent was still working when a comment
  was posted. (H) A person stops a cloud agent in the agent's own app; nothing
  reaches GitHub. (I) Permission prompts are answered in the agent's own app.
* (J) GitHub names the account behind a commit, and cloud agents often push as
  the person who started them, so a person's commit cannot be told from the
  agent's.
* (K) Claude Code's VS Code extension sends no `permission_prompt`
  notification, so there only the transcript's denials are seen.

Not covered by a row: Codex inside an IDE extension and Cursor's CLI (the
hooks are the same configuration, but neither has been verified), Copilot,
Devin, OpenHands and other agents without a Core adapter (only `web`
applies), and a local devcontainer (it reads as the machine it runs on).

### Environment on every block

Each block carries `environment`, from the Receipt's own where-it-ran
evidence (`capture.agent_host`, `adapters/agent_env.py`: the hook process's
environment, never guessed from the agent's name):

| `environment` | From |
|---|---|
| `cloud_agent` | the agent's hosted-VM marker: `CLAUDE_CODE_REMOTE` (Claude Code on the web), `CURSOR_CODE_REMOTE` (Cursor cloud agents) |
| `cloud_workspace` | the workspace's own variable: `CODESPACES`, `GITPOD_WORKSPACE_ID`, `REPLICAS_WORKSPACE_ID` |
| `ci` | a CI runner's own variable (`GITHUB_ACTIONS`, `GITLAB_CI`, `BUILDKITE`, `CIRCLECI`, `JENKINS_URL`, `CI`) |
| `local_ide` | local, and the app is VS Code (`claude-vscode` entrypoint or `TERM_PROGRAM=vscode`) or JetBrains |
| `local_terminal` | local, and the app is a terminal (Claude Code's `cli` entrypoint, iTerm2, Terminal, WezTerm, Ghostty, Warp, tmux, Hyper, kitty) |
| `web` | only the GitHub-derived block: the person steered on GitHub, where OpenShard had no runtime presence |

When the evidence does not say, there is no `environment`, and
`environment_not_recorded_reason` says why: `where_it_ran_not_recorded`
(no hook environment reached Core, e.g. only HTTP hooks, which carry none),
`local_app_not_recorded` (local, app unknown: Codex or Cursor launched
from an IDE or an app that sets no terminal variable), or
`local_app_not_terminal_or_ide` (headless SDK, the Claude desktop app).
Where it ran is now recorded for every hook-captured agent, not only Claude
Code (`ran_on` / `agent_app` in sync).

### Remote and cloud runtimes

Inside a cloud agent VM or cloud workspace, Core runs the same hooks and reads
the same session log, so the steering facts are the same facts, classified
inside that runtime: only `prompt_facts` (intent, confidence, rule, length)
are queued, and log messages are classified at fold time and dropped. Every
steering fact has a timeline Event (`session.activity`, or the hook's own
Event) with `steering_kind`, `intent` / `decision` where it has one, and
nothing else; those Events stream out through the remote capture journal
(`docs/remote-capture.md`) as they are folded, including the facts read from
the log during the fold that seals a turn or ends the session. So if the VM
is destroyed before its Receipt is delivered, the journal still holds what
was steered. The delivered Receipt carries the block as everywhere else.
The spool's own privacy rules (`remote/spool.py: wire_event`) apply on top.

### Pull requests (`source: github`)

Where OpenShard has no runtime presence, the cloud-receipts workflow
(`.github/workflows/openshard-cloud-receipts.yml`) derives steering from the
pull request each pushed commit belongs to (`github/steering.py`, read-only
`gh api` GETs):

| On GitHub | Event |
|---|---|
| a review requesting changes | `follow_up`, `via: review_changes_requested`, intent `correction` (rule `github.review_changes_requested`) |
| an approving review | `follow_up`, `via: review_approved`, intent `approval` |
| a review that comments | `follow_up`, `via: review_commented`, intent from `rules-v1` on its text (or its first inline comment's) |
| a PR comment addressed to the agent (`@codex`, `@claude`, `@copilot`, `@cursor`, `@devin`, `@jules`, `@grok`, `@openhands`, `@gemini`, `@amp`) | `follow_up`, `via: agent_mention`, intent from `rules-v1` on the text without the mention |

plus `github` counts: `review_comments` (inline, by people), `review_rerequests`
(a review requested again from someone who had reviewed), `bot_comments_ignored`
(a bot is not a person), and `commit_after_review` (this commit followed a
review: the follow-up push). Each commit's Receipt carries the input after the
previous commit of the PR and up to this one, by committer date (GitHub does
not record when a commit was pushed); input after the last commit is on no
Receipt until a commit follows. Pending and dismissed reviews, PR comments not
addressed to an agent, and comments by bots are not steering. The text is
classified inside the CI job and dropped; no comment text and no login is in
the block. It goes to the Platform as an optional `steering` key on the
GitHub receipt; a Platform that rejects the key gets the Receipt without it
(the workflow resends), never a failed delivery.

## The steering event

Each fact is one entry in `capture.steering.events` (bounded to 100; the
overflow is counted in `events_dropped`). For the timeline, a hook-observed
prompt's existing `session.activity` Event gains `steering_kind` and `intent`;
a hook-observed interrupt's Event and a person's Hermes approval Event gain
`steering_kind`; log-observed interjections, interrupts and rejections and
git-observed manual edits add a `session.activity` Event of their own.

| Field | Values |
|---|---|
| `at` | UTC time. Hook events: whole seconds; log events: the log's milliseconds |
| `turn_index` | the Receipt's turn (per-turn Receipts) or the session's turn count so far; absent when unknown |
| `kind` | `initial_prompt`, `follow_up`, `interjection`, `interrupt`, `permission_decision`, `manual_edit` |
| `intent` | follow-ups and interjections: `correction`, `redirect`, `clarification`, `approval`, `continue`, `new_task`, `unknown` |
| `intent_confidence`, `intent_rule` | `high` / `medium` / `low`, and the id of the rule that decided |
| `after` | `turn_end`, `failed_check` (the turn ended on a failed check), `tool_call`, `permission_prompt`, `none`, `unknown` |
| `source` | `hook`, `transcript` (the agent's own log), `git`, `github` (a pull request) |
| `via` | GitHub only: `review_changes_requested`, `review_approved`, `review_commented`, `agent_mention` |
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

**OpenShard reads these agents' own local logs on the machine where the agent
runs, to count and classify steering; the text never leaves that machine.**
The logs read: the Claude Code transcript, the Codex rollout, Cursor's agent
transcript and its local `state.vscdb` (read-only), Antigravity's
`transcript.jsonl`, Grok Build's `updates.jsonl` and Hermes' `state.db`
(read-only). Each is validated as this session's own log before it is opened
(a path under the agent's own directory, naming the session), read in bounded
chunks, and never copied: its locator stays on the transient staging buffer,
exactly like `transcript_path` before. Only the facts reach the Receipt:
kind, intent, confidence, rule id, length in characters, times, a decision,
and the log format read (`log_formats`, e.g. `hermes-state-db/v32`;
`unversioned` where the agent marks no version).

The prompt text is read only where the agent runs (the user's machine, the
cloud VM or workspace, or the CI job that reads a pull request), to classify
it, and is dropped at once. Hook prompts are classified where the capture service reduces
the hook (`reduce_hook_payload`); the queue line carries only `prompt_facts`
(`intent`, `confidence`, `rule`, `chars`). Log messages are classified at
fold time and never written anywhere. Nothing steering adds holds prompt
text: not `runs.jsonl`, not the Receipt, not the sync payload, not telemetry
(steering adds no telemetry property).

**Only the task's first prompt names a Receipt.** The prompt that starts a
task (the first prompt of an agent session) is kept as the task, a
secret-scrubbed excerpt of at most 300 characters (`task`, `task_title`;
`session.task_source: first_user_prompt_excerpt`), stored and synced as
before. A Receipt that continues the task (the next turn's Receipt, or the
segment a resumed session opens) holds no prompt text at all: its task and
title are the task's first title plus the turn number (`Revamp website hero
· turn 3`; `· continued` when the turn number is not known), with
`capture.task_source: task_title_and_turn`, `capture.task_base_title` and
`capture.task_turn` (`adapters/task_continuation.py`). Sync sends no
`session.task_source` for it (the value is not in the Platform contract yet:
unknown, never mislabelled). A later prompt in the same Receipt never
becomes its task either, even when the first had no text. A task that ended
(a branch change, its pull request merged or closed) is not continued: the
next prompt starts a new task and names it. Receipts sealed before this rule
keep the excerpt they were sealed with; nothing is rewritten. GitHub-derived
Receipts were already free of prompt text (their title is the commit
subject; review comments are classified and dropped).

`tests/test_steering_capture.py::TestPrivacy` feeds prompts holding a
secret-shaped key and distinctive phrases through Claude Code and Codex and
checks every file under `.openshard/`, `runs.jsonl`, the sync payload, the
rendered Receipt and the telemetry queue: a follow-up's phrase appears
nowhere, while the first prompt's scrubbed title does.
`tests/test_steering_agent_logs.py::TestPrivacy` does the same for every newly
read log (Cursor's transcript and store, Antigravity, Grok Build, Hermes, and
the OpenCode plugin's mid-turn message). `tests/test_steering_environments.py`
does the same for a cloud session (every file under `.openshard/` and the
OpenShard home, including the remote capture journal) and for the GitHub path
(the block, the payload the workflow sends, and what `python -m
openshard.github.steering` prints).

## Autonomy (derived)

`history/steering.py` derives these from the stored, sealed block; nothing
is computed from text.

| Fact | Meaning |
|---|---|
| `human_inputs`, `human_inputs_by_kind` | times a person stepped in; a kind the agent does not expose is `null` |
| `intents` | follow-ups and interjections by intent |
| `interrupts` | Esc / stop |
| `permission_prompts` | `asked` / `allowed` / `denied`; `null` where the agent does not record it (see "What each count can say" above). Which counts a capture could make is stored with the block (`permission_counts`, local only), so a block recorded before them reads back exactly as it was sealed |
| `longest_unsupervised_seconds` | the longest time the agent worked with no human input, over turns whose end was observed; `null` when none was |
| `steering_seconds` | sum of think time between a turn end and the next prompt, each pause capped at 15 minutes |

Where it ran is the block's `environment` (derived from the Receipt's existing
`capture.agent_host`, which sync sends as `ran_on` / `agent_app`); the
permission mode is not repeated: it is `runtime_configuration.permission_mode`.

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
  `paths`, `turn_ends` and `permission_counts`, at most 50 events, plus
  `autonomy` and, where an agent's log was read, `log_formats`. It is optional
  in the receipt-sync contract (`tests/fixtures/platform/receipt-sync-envelope.v1.json`),
  proposed by Core; a Platform that does not know it rejects the key and Core
  resends the Receipt without it (`OPTIONAL_RECEIPT_KEYS`), keeping it locally.

## Limits

* Hook times are whole seconds; two facts within one second may be ordered
  by arrival rather than by the instant they happened.
* No agent's log format is a stable API. The shapes read here (Claude Code
  `queue-operation`, `[Request interrupted by user]`, the rejection text;
  Codex `event_msg` `task_started` / `user_message` / `turn_aborted` and the
  `rejected by user` tool output; the Cursor, Antigravity, Grok Build and
  Hermes shapes in `adapters/steering_logs.py`) are each agent's documented
  or widely observed ones; an unknown shape, an unreadable database or a
  broken line under-reports, never invents and never breaks capture.
* A Claude Code Esc fires no hook, so no turn-end snapshot is taken: manual
  edits right after an Esc are not seen.
