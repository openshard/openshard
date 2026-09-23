# Agent capture: Claude Code, Codex, Cursor, OpenCode, Google Antigravity and Hermes Agent

Openshard records the coding-agent work you already do. Six agents feed
**one** capture path:

```text
Claude Code hooks (HTTP + SessionStart command) ─┐
Codex hooks (command)  ──────────────────────────┤
Cursor hooks (command) ──────────────────────────┤
Antigravity hooks (command) ─────────────────────┤
Hermes shell hooks (command) ────────────────────┼──> local capture service (127.0.0.1, authenticated)
OpenCode plugin (fetch) ─────────────────────────┘        POST /hooks/{claude,codex,cursor,antigravity,hermes,opencode}
                                                               │  blocking path: authenticate -> validate -> translate -> reduce -> fsync queue -> 200
                                                               ▼  background: replay through the shared fold (undecodable lines quarantined)
                                                    canonical Events -> Run/Attempt -> Shard -> Receipt
                                                    (.openshard/runs.jsonl, one history per repository)
```

## Trust boundary and authentication (v0.4.4)

The service binds to loopback only, but loopback is not a user boundary:
another local account, a sandboxed process or a web page's cross-origin
`fetch` can all reach `127.0.0.1`. So every `POST` is authenticated, and
`GET /health` -- the only open endpoint -- authorises nothing.

| Credential | Where it lives | Who presents it | Authorises |
|---|---|---|---|
| **Capture token** -- 64 hex chars, random, generated locally on first use | `<OPENSHARD_HOME>/capture-token` (`~/.openshard/capture-token`), mode 0600, never inside a repository | our own processes only: `openshard hooks claude` (SessionStart), `openshard hooks claude-status`, `openshard hooks codex`, `openshard hooks cursor`, `openshard hooks antigravity`, `openshard hooks hermes`, `openshard capture stop` | every endpoint, including `/shutdown` |
| **Scoped capability** -- `r2.` + HMAC-SHA256(token, normalised repo root + `\n` + agent key) | third-party configuration that runs no process of ours at delivery time: the `X-OpenShard-Capture-Token` header of the Claude Code HTTP hook entries in `.claude/settings.local.json` (agent `claude_code`); the `CAPABILITY` constant in `.opencode/plugins/openshard.js` (agent `opencode`) | Claude Code's HTTP hooks; the OpenCode plugin | events **for that repository and that agent only**; never shutdown |

The service checks a capability against the agent the receiver path records
under (`/hooks/claude` -> `claude_code`, `/status/claude` -> `claude_code`,
`/hooks/codex` -> `codex`, `/hooks/cursor` -> `cursor`, `/hooks/antigravity`
-> `antigravity`, `/hooks/hermes` -> `hermes`, `/hooks/opencode` -> `opencode`), so a leaked Claude capability for repo A cannot submit
Cursor, Codex or OpenCode events for repo A, a Cursor capability cannot
submit Claude events, and no capability for repo A works for repo B.
Codex, Cursor, Antigravity and Hermes need no capability: their hooks run
`openshard hooks codex|cursor|antigravity|hermes`, a process of ours that reads the
token file. The master
token never appears in any agent's configuration.

Rules the service enforces (`adapters/claude_capture_service.py`,
`adapters/capture_auth.py`):

* No plausible credential -> `401` before the body is parsed; nothing is
  recorded; `stats.rejected` increments. A credential for a different
  repository -> `401` as well.
* `Origin` / `Referer` / `Sec-Fetch-Site` present -> `403` (browser defence
  in depth; the primary check is still the credential).
* `/shutdown` needs the token *and* the instance id; the instance id in
  `/health` is informational and cannot authorise anything on its own.
* The token never appears in telemetry (the schema has no free-text
  field), in logs, in `/health`, or in normal CLI output.
* `openshard capture rotate-token` replaces the token and invalidates
  every repository capability; re-run `openshard setup` in each Claude
  Code repository.

Why a capability lives in a repository-local file at all: Claude Code's
documented HTTP hooks can send static headers or interpolate a variable
from Claude Code's own process environment, and nothing else; the OpenCode
plugin is a file OpenCode executes. The installers keep both files out of
git (`.git/info/exclude`) and refuse to write a credential into one git
tracks. A leaked capability can only inject events for that one
repository and that one agent, and cannot stop the service.

One side effect worth knowing: Claude Code's CLI fires a `SessionEnd`
HTTP hook even for `claude mcp get` / `claude mcp list`. `openshard
doctor` and `openshard setup` run `claude mcp get openshard` to detect the
MCP registration, so on a repository whose Claude hooks still lack a valid
capability each `doctor` run adds one refused request to the service's
counter. That refusal is correct (a credential-less hook) and harmless (a
`SessionEnd` with no work is never recorded); it disappears once the hooks
are upgraded.

**Migration.** Hooks installed before v0.4.4 carry no credential and are
refused by an upgraded service. `openshard setup` rewrites the Claude hook
entries and the OpenCode plugin; a `SessionStart` of an upgraded Openshard
also upgrades the repository's Claude hook entries itself (Claude Code
snapshots hooks per session, so the fix applies from the next session, and
the in-process fallback fold still records the current one). `openshard
doctor` reports Claude hooks and OpenCode plugins with a missing or stale
credential and shows the service's `refused` counters.

**Agents stay fail-open.** A refused or unreachable service never blocks
the agent: command hooks exit 0 (Cursor still receives its decision reply),
HTTP hook failures are non-blocking in Claude Code, the OpenCode plugin
buffers and retries. Only evidence is lost, and the loss is counted.

## Change attribution (v0.4.4)

Git proves the repository changed. It does not prove which process changed
it, so the fold no longer calls every difference since session start
"changed by the agent". At the first observed hook (SessionStart where the
agent has one) the service snapshots the working tree (`git status
--porcelain -z --untracked-files=all` plus blob ids, bounded to 500 paths)
into the session's baseline. At every fold each path in the git diff is
classified:

| `attribution` | Meaning | Counted in `Changed N files`? |
|---|---|---|
| `agent_reported` | the agent's own positive success signal names this path (Claude `PostToolUse` success, Cursor `afterFileEdit`, OpenCode `file.edited`) | yes |
| `git_observed` | the repository changed; no agent signal; actor not established (a human, another tool, or an agent without a success signal such as Codex) | yes, labelled as git-observed |
| `pre_existing` | already dirty/untracked at session start and content unchanged since | **no** -- listed as excluded |
| `other_session` | reported by another live agent session in the same repository | **no** -- listed as excluded |

A pre-existing file that changed again during the session is
`git_observed` with `pre_existing: true` (never hidden); a pre-existing
file the agent reports editing is `agent_reported` with the same flag. A
Codex `apply_patch` target that appears in the diff is `git_observed` with
`agent_attempted: true` -- Codex reports the attempt, not its success.

The record carries `files_detail[].attribution` and a `changes` block
(counts, `baseline.source`, `baseline.dirty_paths`, `baseline.truncated`,
`files_truncated`). The receipt reads `Changed  2 files (1 agent-reported;
1 git-observed, actor not established)` with separate `Pre-existing` /
`Other session` exclusion rows; `--full` lists the excluded files and the
baseline. Limits: a session whose first observed hook is not a start hook
(Cursor background agents) gets its baseline at that first hook; an
*ended* sibling session's files become `git_observed`, never this
session's.

## Capture depth and completeness (v0.4.4)

Two separate questions, kept separate in the record, the JSON and the
receipt:

* **Capture depth** (`capture_depth`, unchanged): how deep Openshard could
  ever observe or control the run -- `full` (Openshard ran it), `partial`
  (observed through an agent's hooks; Openshard did not execute or verify),
  `unknown`.
* **Completeness** (`capture.completeness.status`): within the evidence the
  integration is expected to deliver, is any evidence *known* to be lost?

| `status` | Meaning |
|---|---|
| `complete` | every loss detector stayed at zero -- not a claim that nothing was missed |
| `incomplete` | evidence is known lost; `reasons[]` says why |
| `unknown` | cannot be established: a record written before loss tracking (0.4.3 and earlier hook records) or of unknown origin |

Examples: a healthy Claude Code session is `depth = partial, status =
complete`; the same session with one undecodable queued event is `depth =
partial, status = incomplete`; a native run with no known loss is `depth =
full, status = complete`; a 0.4.3 hook record is `depth = partial, status
= unknown`. There is no score.

Reasons: `corrupt_queued_event` (a durable queue line could not be
decoded; the bytes are kept, bounded, under
`.openshard/claude_sessions/quarantine/` and the service counts
`corrupt_lines`), `dropped_hook_events` (buffer cap of 200 staged events
reached), `session_end_not_observed` (the buffer was swept after an hour
idle without a SessionEnd), `integration_limitation`. Valid lines next to
a corrupt one are still applied; transient I/O errors keep the retry path
and are never counted as corruption. The compact receipt keeps the usual
`Capture  partial — OpenShard did not execute or verify this run` line and
adds `Gaps  None known` / `Gaps  1 queued event could not be decoded` /
`Gaps  Unknown (record predates loss tracking)`; the full receipt has a
CAPTURE section with `Capture depth`, `Completeness` and `Known gaps`.
JSON carries `capture_completeness = {depth, status, reasons, derived}`.
Records written before v0.4.4 derive their status at read time and are
labelled `derived`.

## Identity (v0.4.4)

Every new record carries `receipt_id` (`rcpt_` + 32 hex; a UUID4),
minted at creation and safe across repositories, machines and
organisations. `shard_id` (`shard-YYYYMMDD-NNNN`, history position) is
unchanged and remains the grouping key for attempts. `task_id`
(`history/task_identity.py`) is a third, additive identity: one explicitly
declared engineering task across attempts, agents and potentially
repositories. It is minted only by `openshard task new` and attached with
`--task-id`; it is never inferred from prompt similarity, timing, or
`shard_id`, and old records without one remain fully valid. Owner /
Requested by / Approved by are not recorded locally and are never inferred
from git config or the OS user; only the executing agent is known.

Nothing in the fold, the Shard model, the receipt renderer, `history`/
`context`/`relevant_context` or the MCP server was redesigned for
multi-agent capture. What was added is the smallest thing that lets the
existing fold serve several producers: a static agent-profile table, one
translator and one installer per agent, and per-agent readiness in
`setup`/`doctor`. Cursor's translator is documented in
`adapters/cursor_hooks.py`.

## Agent identity (never inferred from the model)

| Agent | `executor` | Receipt "Executor" | `capture.agent` / `agent_vendor` | Provider / model |
|---|---|---|---|---|
| Claude Code | `claude_code_hooks` | Claude Code (external) | `claude_code` / Anthropic | model from the status line (unchanged) |
| Codex | `codex_hooks` | Codex (external) | `codex` / OpenAI | model slug from every hook payload; provider **not** exposed, so not recorded |
| OpenCode | `opencode_plugin` | OpenCode (external) | `opencode` / — | `providerID/modelID` OpenCode reports on the user/assistant message, only when present |
| Google Antigravity | `antigravity_hooks` | Google Antigravity (external) | `antigravity` / Google | `modelName` from every hook payload; every distinct model kept; provider **not** exposed (Antigravity also runs non-Google models), so not recorded |
| Hermes Agent | `hermes_hooks` | Hermes Agent (external) | `hermes` / Nous Research | `model` and `provider` Hermes reports on its request hooks (`provider/model` slug), only when present; token counts as Hermes reports them; no cost |

Every one of these is `origin = external_observed`, `capture_depth =
partial` (`history/shard.py`): Openshard observed the session, it did not
execute or verify it. `executor == "opencode"` (no `_plugin`) still means
Openshard *routed* work to OpenCode itself and is unaffected.

The profile table is `openshard/adapters/capture_agents.py`. The fold in
`adapters/claude_hooks.py` stores the agent key on the staging buffer and
looks every label up from the profile; it never branches on an agent name.

## Canonical event mapping

| Agent event | Openshard event | Canonical Event(s) staged | Evidence |
|---|---|---|---|
| Codex `SessionStart` / OpenCode `session.created` | `SessionStart` | `session.started` (first hook) | directly_observed |
| Codex `UserPromptSubmit` / OpenCode `chat.message` | `UserPromptSubmit` | `session.activity` "user prompt submitted"; first prompt becomes the task excerpt and mints the Shard | directly_observed |
| Codex `PostToolUse` / OpenCode `tool.execute.after` | `PostToolUse` | `tool.invoked` (file tools: target = repo-relative path the agent *tried* to change, status **unknown**; shell: summarized command, status unknown; other tools: name only) | agent_reported |
| OpenCode `file.edited` | `FileEdited` | none (OpenCode publishes it only after a successful write, so the path is kept for the git-unavailable fallback) | — |
| Codex `Stop` | `Stop` | `session.activity` "assistant turn completed"; fold | directly_observed |
| OpenCode `session.idle` | `SessionIdle` | `session.activity` "session idle (turn completion not confirmed)"; fold; **never** a completed turn (`turn_count` untouched, `idle_count` incremented) | directly_observed |
| Codex `Interrupt` | `Interrupt` | `session.activity` "turn interrupted by user"; fold | directly_observed |
| Codex `SessionEnd` / OpenCode `session.deleted` | `SessionEnd` | `run.completed` status **unknown**; fold; buffer removed | directly_observed |
| OpenCode `message.updated` (assistant, completed) | usage report | none; provider/model, cost and tokens recorded per message id | agent_reported |
| fold (every Stop/SessionEnd, throttled tool hooks) | — | `file.changed` from `git diff` against the session-start HEAD, each with `metadata.attribution` (see *Change attribution*) | git_observed |

Codex's `apply_patch` names its files in patch headers (`*** Add File:`,
`*** Update File:`, `*** Delete File:`, `*** Move to:`); only those header
lines are read, never the patch body, and they name the files Codex
*attempted* to change (the `tool.invoked` target), never file evidence.
MCP tools (`mcp__server__tool`) are recorded by name only.

### Codex payload audit (what is read, on what authority)

Confirmed against OpenAI's Codex hooks reference
(`developers.openai.com/codex/hooks`):

| Field | Status | How Openshard treats it |
|---|---|---|
| events `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, `SessionEnd`, `Interrupt` | documented | subscribed; `Interrupt` is activity, never completion |
| `PostToolUseFailure` | **does not exist** in Codex (`PostToolUse` also fires for non-zero Bash exits) | a document naming it is not a Codex hook; a Codex `PostToolUse` is never a success signal |
| `session_id`, `cwd`, `hook_event_name`, `model` | documented | read (model slug preserved, bounded) |
| `tool_name` = `Bash` | documented hook-facing name for shell commands, including `exec_command` / unified-exec completions | command tool; `tool_input.command` (string) summarized |
| `tool_name` = `apply_patch` | documented | file tool; patch envelope read from `tool_input.command`, headers only |
| `tool_name` = `Edit` / `Write` | matcher aliases only; hook input still reports `apply_patch` | name-only record, no file evidence |
| `shell`, `exec_command`, `local_shell`, `shell_command`, `unified_exec` as `tool_name` | internal names, not hook-facing | name-only record, never a command |
| `SessionEnd.reason` | documented (currently only `other`) | read; `end_reason` is **not** read |
| `SessionEnd` / `Interrupt` timeouts | documented: default 1 s, max 3 s | hooks installed with 3 s and `--no-spawn` |
| `async: true` on command hooks | documented (SessionEnd always synchronous) | `PostToolUse` installed async |
| `tool_input.command` as an argv list | pre-hooks tool shape, unconfirmed on the wire | tolerated: joined into the scrubbed command summary only |
| `tool_input.patch` | community hook templates only, unconfirmed | tolerated after `command`; headers only |
| `prompt`, `stop_hook_active` | Claude Code field names Codex's vocabulary mirrors, not shown in the reference examples | `prompt` feeds only the scrubbed task excerpt; `stop_hook_active` is carried, never acted on |
| `turn_id`, `tool_use_id`, `permission_mode`, `tool_response`, `transcript_path` | documented | never read |

Every unconfirmed shape can only *under*-report: a malformed or unknown
`tool_input` yields a tool record with no file targets and no command,
never invented evidence.

## Evidence and privacy semantics (fail closed)

* **Verification is never recorded** for any of the three agents. A test
  command is a `tool.invoked` with `command_kind = test`, not a
  verification result.
* **File-tool success needs a provider signal.** Claude Code documents
  `PostToolUse` as firing only after a tool completed successfully
  (failures go to `PostToolUseFailure`), so a Claude file edit is
  `passed` and joins the hook-reported file list -- unchanged. Codex's
  `PostToolUse` also fires for failed commands and OpenCode's
  `tool.execute.after` carries no outcome, so their file tools are
  recorded as `unknown` (target = the attempted path) and contribute
  **no** hook-reported paths; only OpenCode's `file.edited` (published
  after a successful write) does. Git-observed changes are evidence on
  their own for every agent.
* **Idle is not completion.** OpenCode's `session.idle` also follows an
  aborted turn, so it never increments `turn_count`, never sets
  `last_turn_completed_at` / `duration_seconds`, and the record's
  `task_status` stays `in_progress` (or `ended_no_turn` after
  `session.deleted`); `capture.idle_count` / `last_idle_at` record what
  was seen. OpenCode exposes no positive "turn finished successfully"
  signal the plugin forwards, so none is claimed.
* **Cost / tokens**: Claude Code from the status line (`provider_reported`,
  unchanged). Codex hooks expose neither, so a Codex record never carries
  `estimated_cost`/`prompt_tokens` (receipt shows *Not recorded*). OpenCode
  reports `cost` and `tokens` on each assistant message; the buffer keeps
  the latest report per message id and sums them, so a message re-reported
  while streaming replaces rather than adds; stamped `agent_reported` and
  displayed as an estimate. OpenCode reports `cost: 0` when it has no
  pricing for the model, which is "unknown", not "free": a cost is
  recorded only when at least one per-message cost is strictly positive
  (the sum of the positive ones, a lower bound); otherwise the receipt
  shows *Not recorded*, never `$0.00`. Tokens are kept either way.
* **Model / provider**: preserved as the agent reports them, sanitized and
  bounded. A provider is never guessed from a model name; OpenCode's record
  is `provider/model` only when OpenCode itself exposed both.
* **Never stored**: transcripts / `transcript_path`, `tool_response` /
  tool output, `last_assistant_message`, patch bodies, tool arguments other
  than the file path / command, absolute paths outside the repository,
  environment variables. Prompts are reduced to a secret-scrubbed, 300-char
  excerpt of the *first* prompt (the Shard task); commands to a scrubbed,
  100-char summary. The OpenCode plugin already truncates what it sends
  (400 chars) and never reads tool output.
* **Session boundary**: one agent session = one Shard, attempt 1 — exactly
  the Claude rule. Sessions are never grouped by prompt text or timing, and
  two agents' sessions never merge: staging buffers and capture-service
  queue files are scoped per agent (`<agent>.<sid>.json`,
  `<agent>.<sid>.queue.jsonl`; Claude Code keeps its pre-PR12 names) and
  records are upserted by `(executor, capture.session_id)`. Pre-PR12
  buffers and queue lines carry no `agent` field and are read as Claude
  Code sessions.
* **Repository isolation**: Codex's `cwd` and OpenCode's `worktree` resolve
  to the nearest git root exactly as Claude's `cwd` does; the home
  directory is refused as a capture root; `.codex/` and `.opencode/` join
  `.openshard/`/`.claude/` as local state that is never counted as the
  task's changed files.

## Codex integration

* **Config**: project-local `<repo>/.codex/hooks.json` (the same
  matcher-group layout Claude Code uses, so `merge_openshard_hooks` /
  `remove_openshard_hooks` are reused with Codex specs). Created files are
  added to `.git/info/exclude`; a pre-existing file is merged into and left
  to the user's git.
* **Hooks** (all `type: command`, `openshard hooks codex`): `SessionStart`
  (15s; starts the service), `UserPromptSubmit` (5s), `PostToolUse` (5s,
  **async** so Codex's tool loop never waits), `Stop` (5s), `SessionEnd`
  and `Interrupt` (3s, `--no-spawn`: Codex caps these timeouts, so the
  hook never tries to start a service it could not wait for).
* **Blocking path**: `openshard hooks codex` is dispatched on the fast
  console-script path (no Click import), reads stdin, POSTs the raw
  document to `/hooks/codex`, exits. If no service answers it starts one
  (spawn-coordinated, once) or, with `--no-spawn` / when disabled, folds
  in-process as a fallback. The fold-side translator never imports in the
  hook process while a service is reachable.
* **Trust**: Codex reviews new non-managed hooks once (`/hooks`); the
  installer cannot bypass that and `setup` lists it as a next step.
* **Commands**: `openshard setup` (when `codex` is on PATH),
  `openshard capture install codex`, `openshard capture uninstall codex`.

## OpenCode integration

* **Config**: `<repo>/.opencode/plugins/openshard.js`, OpenCode's supported
  project-local plugin location (loaded automatically; `opencode.json` is
  never touched). The file starts with a marker comment; install only ever
  overwrites a marked file, uninstall only removes one, and a user's own
  file at that path is reported as `skipped_existing`. The plugin is plain
  ESM JavaScript on purpose: OpenCode's CLI runs on Bun, which strips
  TypeScript types, but OpenCode Desktop runs its server under Electron's
  bundled Node, which cannot, so a `.ts` plugin is refused there and never
  loads. Install and uninstall also remove a pre-0.4.5 Openshard-owned
  `openshard.ts` (identified by the marker) so OpenCode never loads both; a
  user's own `openshard.ts` is left alone.
* **Plugin** (`opencode_plugin_install.PLUGIN_SOURCE`, no imports, no
  Openshard logic): observes `session.created` / `session.idle` /
  `session.deleted` / `file.edited` / `message.updated` and the
  `chat.message` / `tool.execute.after` hooks; sends one bounded JSON
  document per observation with `fetch` (1.5s timeout) to
  `/hooks/opencode`. Child (sub-agent) sessions are filtered by
  `parentID`. If the service is unreachable the document joins a bounded
  in-memory buffer (200) and the plugin asks `openshard capture start`
  (fire-and-forget) -- at most once per 60 s, so a service that dies
  mid-session is restarted on the next failed delivery after the cooldown
  and a missing Openshard never causes a spawn storm. The buffer is
  replayed in order by a short timer after each start attempt and by the
  next delivery attempt; with Openshard uninstalled the plugin fails
  silently.
* **Commands**: `openshard setup` (when `opencode` is on PATH),
  `openshard capture install opencode`, `openshard capture uninstall
  opencode`.

## Google Antigravity integration

Sources: Google's hooks reference (`antigravity.google/docs/hooks`, and
`/docs/ide/hooks` for the IDE). Field shapes are cross-checked against
open-source integrations that parse live payloads, because the reference is
terse per event; see the field audit in `adapters/antigravity_hooks.py`.

* **What Antigravity exposes to hooks, and nothing more is claimed.** Five
  command-hook events (`PreToolUse`, `PostToolUse`, `PreInvocation`,
  `PostInvocation`, `Stop`); camelCase stdin with `conversationId`,
  `workspacePaths`, `modelName`, `transcriptPath`, `artifactDirectoryPath`;
  `toolCall.{name,args}`, `stepIdx` and an `error` string (empty on
  success) on `PostToolUse`; `terminationReason`, `fullyIdle` and `error` on
  `Stop`. The event name is not reliably in the payload, so each installed
  command carries `--event <Name>`.
* **Not exposed** (so never recorded, never inferred): the user's prompt
  (the task stays "Google Antigravity session (task not captured)"), token
  counts, cost, the model provider, a session end, a numeric exit code, and
  who approved a tool (Antigravity's own permission prompts are not
  visible to hooks). OpenShard does not read the transcript or artifact
  directory to fill any of these in.
* **Config**: `<repo>/.agents/hooks.json`, a map of hook *names* to event
  configurations; OpenShard owns the `openshard` name only and refuses to
  touch an `openshard` entry holding someone else's command. A file it
  creates is added to `.git/info/exclude`; `.agents/hooks.json` (not the
  rest of `.agents/`, which holds shared rules) is never counted as a
  task's changed file. No `timeout` is written (the documented default is
  30 s).
* **Events** (all `openshard hooks antigravity --event <Name>`):

  | Antigravity event | Openshard event | Recorded |
  |---|---|---|
  | `PreInvocation` (before every model call) | `ModelInvocation` | the first one creates the Shard; each is counted (`capture.invocation_count`); the model is observed, and a `session.activity` "model invoked: <model>" Event is staged whenever it differs from the previous call's |
  | `PostToolUse`, empty `error` | `PostToolUse` | `run_command` -> command (`CommandLine`, scrubbed, `command_kind` test/lint/other, status unknown); `write_to_file` / `replace_file_content` / `multi_replace_file_content` / `client_*_file` -> file write, `passed` and hook-reported (Antigravity's success signal); `view_file` / `view_file_outline` / `view_code_item` / `list_dir` / `client_view_file` -> **read** (repo-relative path, `metadata.access = read`, never a change); anything else (search, browser, MCP) by name only |
  | `PostToolUse`, non-empty `error` | `PostToolUseFailure` | the same tool record, `failed` |
  | `Stop`, no error and not `fullyIdle: false` | `Stop` | completed turn; fold |
  | `Stop`, error or `fullyIdle: false` | `SessionIdle` | snapshot, never a completed turn |

  Not subscribed: `PreToolUse` (a permission gate; any reply but a decision
  denies the tool, and recording never needs to gate) and `PostInvocation`
  (repeats the model and would double the per-call cost).
* **Replies**: `{"decision": "stop"}` for `Stop` (an empty object is
  reported to be rejected there), `{}` otherwise, written whatever capture did. A
  refused or unreachable service never blocks or changes the agent.
* **Session boundaries**: one `conversationId` = one Shard. With no start
  hook, the service anchors the change-attribution baseline when it
  *receives* a session's first `PreInvocation` (one `git status`, once per
  session); with no end hook, the idle sweep (run on each session's first
  invocation, and by every sync run) closes a session after an hour idle and
  its completeness says `session_end_not_observed`. Sync sends the session
  only after that sweep has closed it.
* **Performance**: every event is a process start of `openshard hooks
  antigravity` on the fast console-script path plus a loopback POST whose
  server-side work is validate + reduce + fsync; the fold, git diff and
  `runs.jsonl` write happen on the service's background worker.
  `PreInvocation` runs once per model call, so its cost is the process
  start (tens to a few hundred ms depending on the machine) next to a model
  call that takes seconds; `tests/test_antigravity_capture.py` guards the
  server-side budget like the other agents. Measured on the Linux
  container this was built in (Python 3.11, warm service): real
  `openshard hooks antigravity` subprocess median 42 ms / p95 54 ms per
  event; server-side blocking p50 0.8 ms / p95 1.1 ms; loopback POST alone
  median 2.3 ms. A regression signal, not a universal claim.
* **Known Antigravity limitations**: hook delivery differs by build (some
  CLI builds are reported not to fire `PostToolUse`, and some IDE builds
  not to fire workspace hooks at all). Git-observed changes are still
  recorded whenever `PreInvocation`/`Stop` arrive; if no hook fires, nothing
  is recorded and `openshard doctor` can only show that the hooks are
  configured, not that Antigravity runs them.
* **Commands**: `openshard setup` (when `agy` or `antigravity` is on PATH),
  `openshard capture install antigravity`, `openshard capture uninstall
  antigravity`. Open the repository as the Antigravity workspace (or run
  `agy` in it); restart Antigravity if it was already running.

## Hermes Agent integration

Sources: the Hermes Agent hooks documentation
(`hermes-agent.nousresearch.com/docs/user-guide/features/hooks`, the plugin-hook
catalog and the *Shell Hooks* section), cross-checked against the runtime that
builds the payloads (`agent/shell_hooks.py`, `model_tools.py`,
`tools/file_tools.py`, `tools/approval_context.py`); the full field audit is in
`adapters/hermes_hooks.py`. This integration is **observation only**: Hermes'
`pre_tool_call` hook can block, rewrite or escalate a tool call, and OpenShard
never subscribes it and never returns a directive (every reply is `{}`).

* **Mechanism.** Hermes *shell hooks*: a `hooks:` block in
  `<hermes home>/config.yaml` runs a command per lifecycle event, with one JSON
  document on stdin (`hook_event_name`, `tool_name`, `tool_input`, `session_id`,
  `cwd`, `profile`, and every event-specific field under `extra`). Hermes runs a
  shell hook only after the `(event, command)` pair is in
  `<hermes home>/shell-hooks-allowlist.json`. The hook process is
  `openshard hooks hermes` (fast console-script path), which presents the
  capture token and POSTs the raw document to `/hooks/hermes`; a refused or
  unreachable service never blocks or changes Hermes. `HERMES_HOME` is honoured
  (default `~/.hermes`, `%LOCALAPPDATA%\hermes` on Windows); a Hermes profile has
  its own home, so install once per profile with `HERMES_HOME` set.
* **Scope is user-global; capture is per repository.** Hermes has no
  project-level hook file, so the hooks fire in every directory Hermes runs
  in. Hermes is captured only in a **git repository that already has an
  `.openshard/` directory** (never in an arbitrary folder, never in the home
  directory). `openshard capture install hermes` creates that marker in the
  repository you run it in; run it in each repository you want captured.
  `openshard setup` detects Hermes but does **not** edit Hermes' global config on
  its own: it reports `detected; run openshard capture install hermes`.
* **Config edit.** OpenShard adds one entry per event under `hooks:` (`command:
  "openshard hooks hermes"`, `timeout: 15`, no `matcher`, no `fail_closed`), and
  records Hermes' documented first-use consent in the allowlist file (the same
  entry Hermes writes when a person approves its prompt). When `config.yaml` has
  no `hooks:` key the block is appended between marker comments, so every
  existing byte and comment survives; when `hooks:` already exists it is merged
  and the file re-serialised (comments are not preserved) after a one-time
  `config.yaml.openshard-backup`. Other hooks, `hooks.outbound` and
  `hooks_auto_accept` are never touched; an unparsable file is never written.
  `openshard capture uninstall hermes` removes only OpenShard's entries from both
  files.
* **Events** (Hermes name -> Openshard event -> recorded):

  | Hermes hook | Openshard event | Recorded |
  |---|---|---|
  | `on_session_start` | `SessionStart` | session identity, `model` (new sessions only) |
  | `pre_llm_call` | `UserPromptSubmit` | the turn's `user_message` (scrubbed excerpt becomes the task; text parts only for a multimodal message), `model`. Replies `{}`: no context is injected. The full `conversation_history` is never read |
  | `post_tool_call`, `status: ok` | `PostToolUse` | tool name and arguments as below, `duration_ms`, `tool_call_id`, `turn_id`, `tool_status`; a file tool becomes `passed` and hook-reported (Hermes' success signal) |
  | `post_tool_call`, `status: error` or `blocked` | `PostToolUseFailure` | the same record, `failed` (`blocked` = a policy hook stopped it; it never ran). A failed check command is `agent_reported` failed verification |
  | `post_api_request` | usage observation | per-request `usage` (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) keyed by request id, plus the `model` and `provider` Hermes called |
  | `on_session_end` (fires **every turn**) | `Stop` (`completed`), `Interrupt` (`interrupted`), else `SessionIdle` | a completed / interrupted turn, or a neutral boundary that is never a completed turn |
  | `on_session_finalize` | `SessionEnd` | the real teardown, with Hermes' `reason` |
  | `subagent_start` / `subagent_stop` | `SubagentStart` / `SubagentStop` | child role, child session id, subagent ids, `child_status`, `duration_ms`, the *number* of child tool calls; counted in `capture.subagents` |
  | `pre_approval_request` / `post_approval_response` | `ApprovalRequest` / `ApprovalDecision` | `surface`, `pattern_key`, the scrubbed command, and the `choice`; counted in `capture.approvals` |

  Tool arguments read: `terminal` -> `command` (scrubbed, `command_kind`
  test/lint/other); `write_file` / `patch` -> `path`, or for a V4A patch only the
  `*** Add|Update|Delete|Move File:` header paths; `read_file` -> `path` (a read,
  never a change). Everything else (`search_files`, `execute_code`,
  `delegate_task`, web, browser, skill and MCP tools) is by name only. File
  contents, replacement strings, patch hunks, the tool `result` /
  `error_message`, the delegated goal, the child's summary and the transcript are
  never read.

  Not subscribed: `pre_tool_call` (control), `post_llm_call` (needs the whole
  transcript and repeats `on_session_end`), the stream and auxiliary-call hooks,
  and the gateway, kanban and skill hooks.
* **Evidence classification.**
  * *Directly observed*: the session/turn lifecycle (start, prompt, completed /
    interrupted turn, session end), OpenShard receiving each hook.
  * *Agent reported*: every tool call and its outcome (`status`), model and
    provider, token counts, approvals and their decisions, subagent activity,
    durations, correlation ids, and the *fact that a check command ran*.
  * *Git observed / verified*: the changed files (`git diff` plus a baseline taken
    when the session was first seen, exactly as for the other agents); a
    hook-reported file path is used only as the no-git fallback, and only for a
    tool Hermes reported `ok`.
  * *Independently verified*: never. OpenShard does not run or read the outcome
    of Hermes' checks: an observed check stays `unknown` with
    `outcome_not_observed`, and a check Hermes reported as failed is
    `agent_reported` failed. `verification_passed` stays `None`.
  * *Unknown stays unknown*: absent `status`, `usage`, provider, session id or
    `child_status` records nothing; an approval whose payload has no session id is
    dropped rather than attributed; a `timeout` / `cancelled` / `notify_failed`
    approval is recorded as "not decided", never as a grant or a denial.
* **Provider / model.** `capture.provider` and the `provider/model` slug come from
  Hermes' request hooks only; a hook that names the model without a provider
  never downgrades an already-observed `provider/model`. Hermes is never
  collapsed into its model provider (`agent_vendor` is Nous Research).
* **Tokens and cost.** Token counts are Hermes' own per-request figures, summed
  over distinct request ids (a re-reported request replaces, never
  double-counts) and stamped `agent_reported`; how a provider splits cached
  tokens between `input_tokens` and the cache fields is as Hermes reports it.
  Hermes reports **no cost**, so cost stays Not recorded.
* **Session boundaries.** One Hermes `session_id` = one Shard; `on_session_end`
  is per turn, so a multi-turn conversation accumulates turns in one Shard and
  `on_session_finalize` closes it. A subagent runs as a Hermes session of its
  own: if it emits hooks it becomes its own Shard, linked from the parent's
  `subagent started` Event by `child_session_id`. An interrupted CLI that never
  reaches `on_session_finalize` is closed by the shared idle sweep
  (`session_end_not_observed`).
* **Performance.** Every subscribed event is a process start of `openshard hooks
  hermes`; `post_tool_call` and `post_api_request` fire per tool call / request,
  and `pre_llm_call` carries the conversation history in its stdin (read and
  forwarded, never stored). Server-side work is validate + reduce + fsync; the
  fold runs on the background worker. `tests/test_hermes_capture.py` guards the
  blocking budget like the other agents.
* **Known limitations.** Hermes registers shell hooks when a session starts, so a
  session already running keeps its old hooks; `HERMES_SAFE_MODE=1` and an
  un-allowlisted hook make Hermes skip them (`openshard doctor` reports both,
  and a repository that has not opted in); the task is the first user message of
  the session, which for a gateway platform is whatever Hermes passes as
  `user_message`; the approval hooks carry a session id only when Hermes has
  bound its correlation context.
* **Commands**: `openshard capture install hermes` (works outside a repository;
  inside one it also opts that repository in), `openshard capture uninstall
  hermes`, `openshard doctor` (config, allowlist, safe mode, opt-in), and
  `openshard setup` (detects Hermes, does not edit its config). Start a new
  Hermes session afterwards.

## Setup / doctor / uninstall

* `openshard setup` starts the capture service, configures Claude Code
  (unchanged), then Codex and OpenCode for whichever CLIs are on PATH.
  Readiness is judged across agents: `ready` when at least one detected
  agent is fully configured, `ready_partial` when a limitation was
  recorded (custom status line, user-owned plugin file, an agent CLI
  missing while another is configured), `not_ready` only when no supported
  agent is installed, the repository is unusable, or every install failed.
  `--json` adds `agents.{codex,opencode}` and `configured_agents`;
  `--agent` adds read-only `codex` / `opencode` snapshots.
* `openshard doctor` prints one ✓/✗ section per agent (CLI, integration,
  shared capture service) and a final line naming the agents that are
  ready; `--json` adds `codex` and `opencode` keys.
* `openshard mcp uninstall claude` is unchanged; `openshard capture
  uninstall codex|opencode` remove only Openshard's own hook entries /
  plugin file. History under `.openshard/` is never deleted.

## Performance

`python scripts/bench_agent_capture.py` (same safety rules as
`bench_claude_capture.py`: in-thread ephemeral service, temp
`OPENSHARD_HOME`, `OPENSHARD_CAPTURE_NO_SPAWN=1`, bounded iterations and
wall clock). The design envelope is the PR9.5 one: the caller's blocking
path is a loopback POST whose server-side work is validate + translate +
reduce + fsync; no git, no fold and no `runs.jsonl` rewrite happens before
the response. For Codex the irreducible extra is the `openshard hooks
codex` process start (Codex has no HTTP hook type), which is why
`PostToolUse` is installed async.

Measured on one development machine (Windows 11, Python 3.11, node 24;
`--iterations 100`, run alone). A regression signal, not a universal
claim -- the same machine's Claude numbers are in
`capture-performance.md`, and it carries an unrelated editable-install
import hook that inflates every process start.

| Scenario | n | median | p95 | p99 |
|---|---:|---:|---:|---:|
| codex: POST UserPromptSubmit (python client) | 100 | 15.1ms | 27.1ms | 37.1ms |
| codex: POST PostToolUse Bash (python client; async hook in Codex) | 100 | 15.1ms | 26.8ms | 33.3ms |
| codex: POST PostToolUse apply_patch (python client) | 100 | 14.5ms | 31.4ms | 36.9ms |
| codex: POST Stop (python client; folds in background) | 100 | 20.0ms | 32.8ms | 48.1ms |
| codex: Stop POST returned -> receipt folded in runs.jsonl (not on the caller's path) | 100 | 238.6ms | 571.2ms | 2474.2ms |
| opencode: POST chat.message (python client) | 100 | 8.7ms | 25.1ms | 29.9ms |
| opencode: POST tool.execute.after bash (python client) | 100 | 7.8ms | 25.1ms | 30.2ms |
| opencode: POST message.updated usage (python client) | 100 | 9.2ms | 25.6ms | 37.2ms |
| opencode: POST session.idle (python client; folds in background) | 100 | 20.8ms | 32.5ms | 33.5ms |
| opencode: session.idle POST returned -> receipt folded in runs.jsonl (not on the caller's path) | 100 | 381.3ms | 2571.0ms | 2929.4ms |
| opencode: POST tool.execute.after via node `fetch` (plugin runtime stand-in) | 100 | 7.2ms | 16.7ms | 19.1ms |
| codex: `openshard hooks codex` UserPromptSubmit (real subprocess) | 20 | 356.1ms | 444.2ms | 527.1ms |
| codex: `openshard hooks codex` Stop (real subprocess, synchronous) | 20 | 424.2ms | 514.8ms | 529.6ms |
| codex: `openshard hooks codex --no-spawn` SessionEnd (real subprocess) | 20 | 423.0ms | 482.4ms | 575.5ms |
| server-side blocking time per request, aggregate over the 965 requests above | 965 | p50 3.5ms | 8.0ms | max 19.5ms |

Reading it: the service adds single-digit milliseconds server-side and
~8-20ms end to end over loopback for both agents -- the same envelope as
the Claude HTTP hooks. The OpenCode plugin pays only that. Codex pays the
process start on top (~350-425ms here, the same floor the Claude command
hooks had before PR9.5 and dominated by interpreter/site start-up on this
machine), synchronously for `Stop`/`SessionEnd`/`UserPromptSubmit` and
asynchronously for `PostToolUse`. The fold-behind rows are eventual
consistency: the receipt is normally visible a few hundred milliseconds
after the turn ends; the p99 tail (2-4s) is this box's git/antivirus
contention during the background fold and never blocks the agent.

`tests/test_codex_capture.py::TestServicePath::test_blocking_path_stays_within_budget`
and the OpenCode counterpart guard the server-side p50 < 25 ms / p95 <
50 ms budget in CI, loosely, exactly like the Claude test.

## Tests

* `tests/test_codex_capture.py` — translator (headers-only patch parsing,
  documented vs tolerated `apply_patch` keys, argv commands, internal tool
  names recorded by name only, malformed/unknown shapes under-reporting,
  Windows-style and CRLF patch paths, MCP tools), inline and HTTP records
  (identical stable view), `apply_patch` never `passed` / never
  hook-reported without git, receipt identity, Interrupt, unknown model,
  repo isolation, same session id as a Claude session, agent-scoped
  queue-line privacy, blocking budget, `hooks codex` fast path never
  importing fold code, `--no-spawn` fallback, installer idempotence /
  preservation / malformed config / uninstall, CLI
  install/uninstall/setup/doctor.
* `tests/test_opencode_capture.py` — translator, inline and HTTP records,
  idle never a completed turn (abort/idle regressions), `file.edited` as
  the only hook-reported file signal, `cost: 0` and mixed costs, per-message
  usage dedupe, provider/model identity vs routed OpenCode, the **real
  plugin executed under node** against representative SDK event shapes
  with a stubbed `fetch` (documents fed back through the translator and
  folded) plus the service-down → start → dies → cooldown → restart →
  queued events recover sequence with a bounded queue (the skip reason
  names the node found; `OPENSHARD_REQUIRE_NODE_PLUGIN_TESTS=1` turns a
  missing/too-old node into a failure), installer idempotence / port
  update / preservation of other plugins and `opencode.json` / user-owned
  file / uninstall, CLI.
* `tests/test_antigravity_capture.py` — translator (event from the command
  line, `error`/`fullyIdle` semantics, tool classification, malformed
  shapes, payload agent labels ignored, transcript/contents never read),
  inline and HTTP records (identical stable view), per-model Events,
  read vs write evidence, invocation-only sessions, baseline at receipt,
  idle sweep completeness, receipt identity, sync envelope and telemetry,
  replies, authentication (no token, another agent's capability),
  blocking budget, installer, CLI install/uninstall/setup/doctor.
* `tests/test_hermes_capture.py` — translator (`status` as the only success
  signal, `on_session_end` per-turn semantics, tool classification incl. V4A
  headers, usage keyed by request id, subagent/approval attrs, unsubscribed
  events ignored, hostile shapes), inline and HTTP records (identical stable
  view), tokens without invented cost, approval outcomes never guessed,
  subagent linkage, repository opt-in (plain repo, non-repo, subdirectory,
  other agents unchanged, a not-opted-in answer is never cached), the
  authenticated service path, replies, blocking budget, installer (comment-
  preserving append, merge + one-time backup, idempotence, foreign hooks and
  allowlist entries preserved, refusal on unparsable input, exact uninstall),
  CLI install/uninstall outside a repository, setup (opt-in only), doctor
  readiness states.
* `tests/test_cross_agent_capture.py` — all three agents in one
  repository: distinct Shards and executors, `list_shards` /
  `search_history` / `relevant_context` reach each, receipts keep identity
  and provenance, a shared session id never merges, no cross-repository
  bleed, fail-closed tool semantics per agent (Claude `passed`,
  Codex/OpenCode `unknown`, hook-reported fallback needs a positive
  signal), pre-PR12 buffer / queue-line compatibility, the same through
  one running service, and a crash mid-queue with agent-scoped queue files
  (hook and status lines) replaying into separate Shards.

## Grok Bot (Cursor)

Grok Bot runs on a cloud computer and does **not** use the capture service
above: there are no hooks and no local repository to diff. Enterprise teams
can ingest Cursor's OpenTelemetry Export of Action Recording (`openshard
grok-bot ingest` / `serve`). Those events are observed by Cursor's platform
and recorded `directly_observed` with `metadata.observer =
cursor_action_recording`. Every other plan can only use the self-report skill
(`openshard grok-bot skill` / `report`), whose facts are all
`agent_reported`. See [Grok Bot](grok-bot.md) for the evidence comparison and
the limits.
