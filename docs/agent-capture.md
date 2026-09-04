# Codex and OpenCode capture (PR12)

OpenShard records the coding-agent work you already do. Since PR12 that
covers three agents through **one** capture path:

```text
Claude Code hooks (HTTP + SessionStart command) ─┐
Codex hooks (command)  ──────────────────────────┼──> local capture service (127.0.0.1)
OpenCode plugin (fetch) ─────────────────────────┘        POST /hooks/{claude,codex,opencode}
                                                               │  blocking path: validate -> translate -> reduce -> fsync queue -> 200
                                                               ▼  background: replay through the shared fold
                                                    canonical Events -> Run/Attempt -> Shard -> Receipt
                                                    (.openshard/runs.jsonl, one history per repository)
```

Nothing in the fold, the Shard model, the receipt renderer, `history`/
`context`/`relevant_context` or the MCP server was redesigned. What was
added is the smallest thing that lets the existing fold serve three
producers: a static agent-profile table, two translators, two installers,
and per-agent readiness in `setup`/`doctor`.

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
| fold (every Stop/SessionEnd, throttled tool hooks) | — | `file.changed` from `git diff` against the session-start HEAD | git_observed |

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

* **Config**: `<repo>/.opencode/plugins/openshard.ts`, OpenCode's supported
  project-local plugin location (loaded automatically; `opencode.json` is
  never touched). The file starts with a marker comment; install only ever
  overwrites a marked file, uninstall only removes one, and a user's own
  file at that path is reported as `skipped_existing`.
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
