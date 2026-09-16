# Agent capture: Claude Code, Codex, Cursor and OpenCode

OpenShard records the coding-agent work you already do. Four agents feed
**one** capture path:

```text
Claude Code hooks (HTTP + SessionStart command) ─┐
Codex hooks (command)  ──────────────────────────┤
Cursor hooks (command) ──────────────────────────┼──> local capture service (127.0.0.1, authenticated)
OpenCode plugin (fetch) ─────────────────────────┘        POST /hooks/{claude,codex,cursor,opencode}
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
| **Capture token** -- 64 hex chars, random, generated locally on first use | `<OPENSHARD_HOME>/capture-token` (`~/.openshard/capture-token`), mode 0600, never inside a repository | our own processes only: `openshard hooks claude` (SessionStart), `openshard hooks claude-status`, `openshard hooks codex`, `openshard hooks cursor`, `openshard capture stop` | every endpoint, including `/shutdown` |
| **Scoped capability** -- `r2.` + HMAC-SHA256(token, normalised repo root + `\n` + agent key) | third-party configuration that runs no process of ours at delivery time: the `X-OpenShard-Capture-Token` header of the Claude Code HTTP hook entries in `.claude/settings.local.json` (agent `claude_code`); the `CAPABILITY` constant in `.opencode/plugins/openshard.js` (agent `opencode`) | Claude Code's HTTP hooks; the OpenCode plugin | events **for that repository and that agent only**; never shutdown |

The service checks a capability against the agent the receiver path records
under (`/hooks/claude` -> `claude_code`, `/status/claude` -> `claude_code`,
`/hooks/codex` -> `codex`, `/hooks/cursor` -> `cursor`, `/hooks/opencode`
-> `opencode`), so a leaked Claude capability for repo A cannot submit
Cursor, Codex or OpenCode events for repo A, a Cursor capability cannot
submit Claude events, and no capability for repo A works for repo B.
Codex and Cursor need no capability: their hooks run `openshard hooks
codex|cursor`, a process of ours that reads the token file. The master
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
entries and the OpenCode plugin; a `SessionStart` of an upgraded OpenShard
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

* **Capture depth** (`capture_depth`, unchanged): how deep OpenShard could
  ever observe or control the run -- `full` (OpenShard ran it), `partial`
  (observed through an agent's hooks; OpenShard did not execute or verify),
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
unchanged and remains the grouping key for attempts. Task identity across
retries is a separate, unsolved problem: no `task_id` exists and none is
inferred from prompt similarity. Owner / Requested by / Approved by are not
recorded locally and are never inferred from git config or the OS user;
only the executing agent is known.

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

Every one of these is `origin = external_observed`, `capture_depth =
partial` (`history/shard.py`): OpenShard observed the session, it did not
execute or verify it. `executor == "opencode"` (no `_plugin`) still means
OpenShard *routed* work to OpenCode itself and is unaffected.

The profile table is `openshard/adapters/capture_agents.py`. The fold in
`adapters/claude_hooks.py` stores the agent key on the staging buffer and
looks every label up from the profile; it never branches on an agent name.

## Canonical event mapping

| Agent event | OpenShard event | Canonical Event(s) staged | Evidence |
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

| Field | Status | How OpenShard treats it |
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
  loads. Install and uninstall also remove a pre-0.4.5 OpenShard-owned
  `openshard.ts` (identified by the marker) so OpenCode never loads both; a
  user's own `openshard.ts` is left alone.
* **Plugin** (`opencode_plugin_install.PLUGIN_SOURCE`, no imports, no
  OpenShard logic): observes `session.created` / `session.idle` /
  `session.deleted` / `file.edited` / `message.updated` and the
  `chat.message` / `tool.execute.after` hooks; sends one bounded JSON
  document per observation with `fetch` (1.5s timeout) to
  `/hooks/opencode`. Child (sub-agent) sessions are filtered by
  `parentID`. If the service is unreachable the document joins a bounded
  in-memory buffer (200) and the plugin asks `openshard capture start`
  (fire-and-forget) -- at most once per 60 s, so a service that dies
  mid-session is restarted on the next failed delivery after the cooldown
  and a missing OpenShard never causes a spawn storm. The buffer is
  replayed in order by a short timer after each start attempt and by the
  next delivery attempt; with OpenShard uninstalled the plugin fails
  silently.
* **Commands**: `openshard setup` (when `opencode` is on PATH),
  `openshard capture install opencode`, `openshard capture uninstall
  opencode`.

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
  uninstall codex|opencode` remove only OpenShard's own hook entries /
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
* `tests/test_cross_agent_capture.py` — all three agents in one
  repository: distinct Shards and executors, `list_shards` /
  `search_history` / `relevant_context` reach each, receipts keep identity
  and provenance, a shared session id never merges, no cross-repository
  bleed, fail-closed tool semantics per agent (Claude `passed`,
  Codex/OpenCode `unknown`, hook-reported fallback needs a positive
  signal), pre-PR12 buffer / queue-line compatibility, the same through
  one running service, and a crash mid-queue with agent-scoped queue files
  (hook and status lines) replaying into separate Shards.
