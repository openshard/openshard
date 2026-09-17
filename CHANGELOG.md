# Changelog

All notable changes to OpenShard are documented here.

## Unreleased

### Added

- **`openshard sync`: hosted Receipt history.** `openshard sync connect`
  stores an OpenShard Platform link (endpoint, organisation, `osk_` API
  key) in `~/.openshard/platform.json` (mode 0600, never in a
  repository); `openshard sync now` sends this repository's Receipts as
  the receipt sync envelope v1 (the exact `openshard history --json`
  projection: no prompts, transcripts, diffs, output, notes or paths),
  keyed by `receipt_id`; `openshard sync status` shows what is synced,
  pending, still in progress, changed locally, in conflict or rejected.
  Sending is idempotent and retry-safe: pending work is derived from
  `runs.jsonl` against `.openshard/sync-outbox.jsonl`, a replay is a
  no-op, conflicts and rejections are recorded once and never retried,
  an unreachable Platform backs off exponentially, and an open agent
  session is left alone until it ends or has been idle for an hour. The
  capture service syncs every repository it knows on a timer once a link
  exists. `OPENSHARD_PLATFORM_SYNC=off` or `platform: {sync: false}` in a
  repository's config turns it off. See `docs/platform-sync.md`.
- **`task_id` syncs unchanged.** The receipt sync envelope carries the
  record's explicit `task_id` (v0.4.6) exactly as stored, `null` for
  Receipts that never declared one. Sync never mints or infers one.
- **`repo_identity` in the extended receipt projection.** `openshard
  history --json` now includes the record's canonical `host/owner/repo`
  beside the folder-name `repo` (which hook-captured records never
  carry). The MCP `get_receipt` key set is unchanged.

## 0.4.5 - 2026-09-16

OpenCode capture that loads where OpenCode actually runs, diagnostics that
say only what is proven, and Receipts whose integrity survives OpenShard's
own amendments. No new integrations, no new commands, no telemetry
broadening. Authenticated repo+agent scoped capture, fail-open agent
behaviour, `receipt_id` / `shard_id` semantics and the readability of every
existing on-disk record are unchanged.

Verified end to end on Windows with the real OpenCode Desktop application:
a Desktop session was captured (21 accepted OpenCode deliveries), recorded a
new Receipt with `Executor  OpenCode (external)` and `Integrity  Matches
(content hash)`, and `doctor` reported `✓ Capture verified`. The OpenCode CLI
(Bun) and a Node with TypeScript type stripping forced off are covered by
the test suite.

### Fixed

- **The OpenCode project plugin now ships as plain JavaScript
  (`.opencode/plugins/openshard.js`).** OpenCode's CLI runs on Bun, which
  strips TypeScript types natively, so the previous `openshard.ts` loaded
  there. OpenCode Desktop runs its server in an Electron utility process
  whose bundled Node is compiled without amaro; that Node refuses a `.ts`
  plugin with `ERR_UNKNOWN_FILE_EXTENSION`, OpenCode logs "failed to load
  plugin" and continues, and the session edits the repository while
  OpenShard captures nothing. Plain ESM JavaScript loads under both
  runtimes. Behaviour, bounded payloads, the repo+agent scoped capability
  and fail-open buffering are unchanged; only the extension and the
  (erasable) type annotations differ. Plugin payload version 4 -> 5. The
  node-harness tests now run with type stripping forced off, reproducing
  the Desktop runtime.
- **Authenticated OpenCode Desktop deliveries are no longer refused as
  browser traffic.** With the plugin loading in Desktop, every event it sent
  was still answered `403` before its capability was checked: the capture
  service refuses requests carrying browser-only headers, and its list
  included `Sec-Fetch-Mode`, which Node's undici `fetch` (the runtime under
  Electron) attaches as `Sec-Fetch-Mode: cors` to every request even though
  it is not a browser. Bun's fetch does not, which is why only Desktop was
  affected. `Sec-Fetch-Mode` is dropped from the browser-header set;
  `Origin`, `Referer` and `Sec-Fetch-Site` remain refused outright, with or
  without a token, because a cross-origin browser request always carries
  `Origin`. The repo+agent scoped capability stays the primary gate and
  authentication is not weakened; Claude Code, Codex and Cursor capture are
  unaffected.
- **`openshard setup` / `openshard capture install opencode` migrate an
  OpenShard-owned legacy `openshard.ts` safely.** Install and uninstall
  remove a pre-0.4.5 `openshard.ts` that carries the OpenShard marker, so a
  repository never loads both files (double capture under Bun; a repeated
  load error under Desktop's Node). A user's own `openshard.ts` without the
  marker is never touched. `doctor` / `setup` report a leftover OpenShard
  `.ts` as an outdated plugin and prompt a reinstall instead of reporting
  the integration absent.
- **`doctor` separates "configured" from "capture actually observed".** The
  OpenCode plugin runs inside OpenCode's own runtime, which can silently
  decline to load it (`opencode run --pure`, a Desktop build that cannot
  load the plugin, a stalled plugin-dependency wait, a stale plugin). In
  that state `doctor` previously showed `✓ Capture plugin`. It now adds a
  separate **Capture verified** check that is green only when an OpenCode
  session has actually been recorded in this repository's
  `.openshard/runs.jsonl`, and the summary reads `Configured but
  unverified` until then. `--json` gains `opencode.capture_observed` and
  `opencode.capture_verified`. Capture itself, the translator and the
  capability auth are unchanged.
- **`openshard note` / `openshard feedback` no longer break a Receipt's
  integrity.** Both commands amended the latest record without re-stamping
  `content_hash`, so a fresh Receipt went from `Integrity  Matches` to
  `Integrity  Mismatch` the moment OpenShard itself attached a note. They now
  go through one canonical amendment path (`history.store.amend_latest_record`)
  that verifies the stored hash first, applies the change, records an additive
  `amendments` entry (`kind`, `recorded_at`, `source`, `integrity_before`,
  `content_hash_restamped`) and preserves the integrity verdict: a valid hash
  is re-stamped over the amended content; a legacy record with no hash is
  never given one; a record that already read as mismatched keeps its stored
  hash and stays `Mismatch`. `receipt_id`, `shard_id` and historical content
  are untouched.
- **History amendment and read behaviour is now consistent.** Every CLI
  reader uses one loader (`history.store.load_history`): the last line that
  parses to a JSON object is the latest record, malformed lines are skipped
  on read and preserved byte-for-byte on write, and a legacy record is never
  given a `content_hash` on read (`Not recorded` is never reported as
  `Matches`). The amendment path targets the same record the loader reports
  as latest. `note` and `feedback` also resolve `.openshard/runs.jsonl` from
  any repository subdirectory, the same way `last` / `history` do, and the
  feedback interaction/memory side-records land next to that history instead
  of in the current directory.

### Added

- **`openshard capture status` exposes accepted delivery counts by agent.**
  The capture service counts *accepted* (authenticated and queued)
  deliveries per capture agent, exposed as `stats.by_agent` on `/health` and
  rendered as `by agent: opencode 12, claude_code 40`. Counted only after
  authorization succeeds, so it cannot be spoofed and never weakens auth.
  This is the live signal for "is this agent actually delivering", the
  complement of the persistent per-repository `doctor` check above. No
  secret, path, prompt or repository name is added to any output.

## 0.4.4 - 2026-09-15

Receipt integrity hardening. No new integrations, no new commands beyond
`openshard capture rotate-token`, no telemetry broadening. The goal: a
Receipt never claims more than its evidence supports.

### Changed

- **Changed files are attributed, not assumed.** The working tree is
  snapshotted when a session is first observed; at every fold each path in
  the git diff is `agent_reported` (the agent's own success signal),
  `git_observed` (repository changed, actor not established),
  `pre_existing` (already dirty before the session, unchanged since --
  excluded from counts) or `other_session` (reported by another live agent
  session -- excluded). Receipts read `Changed  2 files (1 agent-reported;
  1 git-observed, actor not established)` with separate exclusion rows;
  `files_detail[].attribution` and a `changes` block are added to records
  and `--json` output. `files_created/updated/deleted` on new records count
  only this session's changes.
- **The local capture service is authenticated.** Every `POST` needs the
  per-user capture token (`~/.openshard/capture-token`, created locally,
  0600) or a capability derived from it and scoped to one repository and
  one agent; requests without one are refused before parsing and counted,
  browser-originated requests are refused, and `/shutdown` accepts only
  the token. Claude Code HTTP hooks carry their capability in a header
  (written by `setup` into the git-excluded `.claude/settings.local.json`;
  a `SessionStart` of the upgraded OpenShard upgrades older hook entries
  itself); the OpenCode plugin carries its own capability; Codex, Cursor
  and the status line run `openshard`, which reads the token file. The
  master token appears in no agent configuration. Agents remain fail-open.
- **Corrupt queued evidence is never forgotten.** Undecodable capture-queue
  lines are quarantined (bounded) under
  `.openshard/claude_sessions/quarantine/`, counted (`corrupt_lines`), and
  the affected record becomes `capture.completeness.status = incomplete`
  with the reason; valid neighbouring events are still applied. Transient
  I/O errors keep the retry path. Receipts keep `Capture  partial` (depth)
  and add `Gaps  None known` / `Gaps  N queued events could not be decoded`.
- **Status wording**: a finished agent turn renders `Turn completed
  (unverified)` (was `Completed`); a session that ended without a turn
  `Session ended (no turn observed)`.
- **Risk** is shown as recorded; the display-time rule that raised a review
  task's missing/Low risk to High is removed.

### Added

- `receipt_id` (`rcpt_` + 32 hex) on every new record, minted at creation;
  shown on receipts and in `--json`/MCP output; accepted by `get_receipt`
  and history search. `shard_id` is unchanged.
- `Integrity` row (`Matches (content hash)` / `Mismatch (content hash)` /
  `Not recorded`) on compact and full receipts.
- `capture.completeness` (`complete` / `incomplete` / `unknown` with
  reasons) on hook records -- separate from the unchanged capture depth
  (`full` / `partial` / `unknown`); records written before loss tracking
  read `unknown`, never `complete`. JSON carries both as
  `capture_completeness = {depth, status, reasons, derived}`.
- `openshard capture rotate-token`; `doctor`/`setup` report hooks with a
  missing or stale credential; `capture status` shows refused and
  quarantined counts. Telemetry `capture.service` gains two bounded
  counters, `rejected` and `corrupt_lines`.
- `docs/architecture.md`, `docs/architecture/V044_RECEIPT_INTEGRITY_AUDIT.md`,
  `docs/architecture/POST_V044_CORE_CLEANUP.md`; rewritten `SECURITY.md`
  and `CONTRIBUTING.md`; trust-boundary, attribution and completeness
  sections in `docs/agent-capture.md`.

### Fixed

- On Windows, several processes opening a brand-new history lock file at
  once could fail with `PermissionError`: the first process seeded and
  locked the sidecar's first byte while a second process's buffered seed
  write landed in that mandatory-locked range. The seed is now written
  unbuffered and a failed seed (which only ever means another process
  already holds the lock) falls through to the normal wait.

### Compatibility

- Records written before 0.4.4 render unchanged: no `receipt_id` is
  back-filled, attribution rows appear only when the record carries them,
  completeness is derived from existing counters. JSON output is extended
  additively; `files_changed` may be lower on new records because
  pre-existing changes are no longer counted.
- Hooks installed by 0.4.3 or earlier are refused by the 0.4.4 service
  until upgraded (`openshard setup`, or automatically at the next Claude
  Code `SessionStart`); the in-process fallback keeps recording meanwhile.
- The test suite now isolates the capture service's default port per test
  and never touches a developer's real service.

## 0.4.3 - 2026-09-12

A DX cleanup pass around the external-agent receipt loop. No commands were
removed or renamed, and no behaviour changed beyond presentation.

### Changed

- `openshard --help` now groups commands into sections (Getting Started,
  Receipts, Diagnostics, Integrations, Advanced) instead of one flat
  alphabetical list. Every command still runs exactly as before; this only
  changes what `--help` shows.
- `openshard setup` is now the one command the README and grouped help point
  new users at. `openshard init` (a lower-level onboarding-preferences
  command) keeps working unchanged; its `--help` text now points to `setup`.
- Terminal receipt output (`openshard last --more/--full`, `openshard report`,
  etc.) reorders existing fields so files/changes and checks appear before
  the policy/approval block, matching the target receipt layout. No fields
  were added, removed, or renamed; `--json` output is unchanged.
- README now leads with the beginner flow (`pip install openshard` ->
  `openshard setup` -> use your coding agent -> `openshard last`); the full
  command-by-command reference moved to `docs/cli-reference.md`.

### Internal

- `hooks claude|codex|cursor|claude-status`, `capture serve`, and
  `shard verify last` are now hidden from `--help` (they are automation
  entrypoints and a documented alias, respectively). They remain fully
  callable and documented in `docs/cli-reference.md`.

## 0.4.2 - 2026-09-12

The clean recovery release that closes the v0.4.x external-agent receipt
chapter. It contains everything in 0.4.1, two intentional additions, and a
fix for the capture service's shutdown path.

**Note on PyPI 0.4.1.** The `openshard==0.4.1` package on PyPI was built from
a local working tree rather than from the `v0.4.1` git tag, and shipped an
early, unreviewed copy of the Cursor and telemetry work below. It has been
yanked. The `v0.4.1` GitHub tag and release are correct and remain valid.
Releases are now built and published only from the pushed tag by the
`release.yml` workflow (see `docs/release-checklist.md`).

### Included from 0.4.1

- All receipt-capture correctness fixes: nested repo-relative paths are no
  longer dropped from changed-file evidence ("Changed 0 files"), and a
  directly observed check command shows as "Attempted (unverified)" rather
  than "Not run". See the 0.4.1 entry below for details.

### Added

- **Cursor support**, alongside Claude Code, Codex and OpenCode. `openshard
  setup` detects Cursor and merges OpenShard's hooks into the project-local
  `.cursor/hooks.json` (unrelated hooks preserved; Cursor reloads the file
  without a restart). New `openshard capture install|uninstall cursor` and
  the `openshard hooks cursor` entrypoint; `doctor` and `setup` report
  Cursor's status like the other agents. Cursor sessions become Shards in
  the same `.openshard/runs.jsonl`, labelled "Cursor (external)", and appear
  in `openshard history`, `openshard context` and the MCP tools together
  with every other agent's work. Every hook is installed fail-open
  (`failClosed: false`), and the one blocking event OpenShard subscribes to
  (`beforeSubmitPrompt`) is always answered `{"continue": true}` whether or
  not capture succeeded: OpenShard observes Cursor, it never gates it.
  Evidence honesty is preserved: Cursor does not expose a tool success
  signal, cost or token counts, so a Cursor receipt records file tools as
  unknown, never claims verification, and shows cost as Not recorded.
- **Telemetry ("Help improve OpenShard"), added intentionally.** The
  telemetry preference is `unset` on every install and nothing is sent in
  that state. Basic privacy-safe telemetry becomes `on` once setup has run: a person sees the
  notice during `openshard setup` or the onboarding flow, and an agent
  running `openshard setup --json` gets the same notice back in the result
  (`telemetry.privacy_notice`, with `telemetry.agent_instruction` telling
  it to show the notice to its owner verbatim), so agent-driven setup can
  never hide it from the person. `openshard setup --agent` stays a
  read-only snapshot that never decides. `openshard telemetry off` turns it
  off immediately and discards the queue. What can be sent is a closed,
  versioned schema of usage and reliability data only: counts, durations,
  the OpenShard version, coarse OS/architecture/Python, fixed category
  values and a random pseudonymous per-install id. Prompts, source code,
  diffs, repository names, paths, file names, commands, model slugs,
  secrets, receipt contents and any other free text are excluded by
  construction -- the schema has no free-text field. `OPENSHARD_TELEMETRY=off`,
  `DO_NOT_TRACK=1`, a CI environment (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`),
  or `telemetry: {enabled: false}` in a repository's `.openshard/config.yml`
  also turn it off, and setup records no decision while one of those is
  set. Richer development data is a separate future level and stays off.
  `openshard telemetry status|on|off|reset|sample`; `sample` prints the
  exact queued events verbatim. The complete contract is in
  [docs/telemetry.md](docs/telemetry.md).

### Fixed

- The capture service could livelock on shutdown when a session's replay
  had a retry pending: the worker re-read its own stop sentinel in a loop
  that never fired the retry, spun until `stop()` gave up on the join, and
  stayed alive as a daemon thread. On Windows the trigger is the antivirus
  `PermissionError` that schedules such a retry; in the test suite the
  leaked thread stalled whole CI jobs (#323). The drain now fires due
  retries itself, waits in bounded slices instead of spinning, and ends at
  a deadline derived from `stop()`'s timeout -- a session that still cannot
  be replayed stays on disk for the next start to recover, as after a
  crash. `serve()` also restores the interpreter's switch interval on exit.

## 0.4.1 - 2026-09-08

Patch release: receipt-capture correctness fixes for Claude Code, Codex,
OpenCode, and `openshard wrap claude`. No new features, no schema or
positioning changes.

### Fixed

- Nested repo-relative file paths (e.g. `evals/basic/bug_fix/fixtures/
  word_utils.py`) were silently dropped from changed-file evidence,
  showing "Changed 0 files" in the receipt even when a tool call had
  genuinely edited the file. The generic secret-scrubbing heuristic used to
  sanitize paths treated an ordinarily nested path's `/`-joined segments as
  a "long opaque key-like run" and rejected it outright. Changed-file
  detection (both git-diff-inferred and agent-reported) now uses a
  dedicated path sanitizer that keeps the specific credential-shaped checks
  (API keys, bearer tokens, `password=...`) without misfiring on ordinary
  directory nesting. Fixes Claude Code hooks, Codex, OpenCode, and
  `openshard wrap claude`, which all shared the same flaw.
- A directly observed test or lint command (e.g. `pytest`, `ruff`) now
  shows as **"Attempted (unverified)"** in the receipt instead of **"Not
  run"**, when OpenShard saw the command execute but never read its
  output. Previously this case was indistinguishable from a session where
  no check ran at all.
- The distinction between externally observed work and OpenShard-verified
  execution is unchanged: OpenShard still never reads a Bash command's
  stdout or exit code for a Claude Code/Codex/OpenCode session, so
  `verification_passed` stays unset (`null`) for these captures regardless
  of what the command's output said. "Attempted (unverified)" reflects only
  that a check-shaped command was seen running — never a pass/fail result.

## 0.4.0 - 2026-09-05

### Added

- Modernized the local MCP server onto MCP SDK v2 (`mcp>=2.0,<3`, replacing
  `mcp>=1.2,<2`'s `FastMCP`/`ValueError` API with `MCPServer`/`ToolError`).
  No change to the server's tools, their arguments, or their output shape.
- PR13 effectiveness benchmark (`evals/pr13/`): a local, no-network harness
  that measures how much OpenShard's captured history actually helps an
  agent, across Claude Code, Codex, and OpenCode, with its own README and
  results summary. Not part of the shipped CLI.
- Codex and OpenCode capture: `openshard setup` now detects and configures
  Codex and OpenCode alongside Claude Code, in any mix, in the same
  repository. Codex hooks are merged into the project-local
  `.codex/hooks.json`; OpenCode gets a small plugin at
  `.opencode/plugins/openshard.ts`. All three feed the same local capture
  service and the same `.openshard/runs.jsonl`, so `openshard history`,
  `openshard context`, and `relevant_context` see every agent's work
  together, each Shard labelled with the agent that produced it. New
  `openshard capture install|uninstall codex|opencode` commands; `doctor`
  and `setup` report each agent's status independently. See
  [docs/agent-capture.md](docs/agent-capture.md).
- Evidence-backed recovery observations (PR11): when `relevant_context`
  ranks a Shard whose most recent verified attempt passed after an earlier
  verified failure, the match now includes a `recovery` observation —
  the failed attempt, the files changed and tools invoked in between, and
  the passing attempt. This is chronology, not causation: OpenShard never
  claims the intervening activity *caused* the pass, only that it was
  observed between the two verification results. No observation is added
  when the Shard's latest verified state is a failure, or when neither
  attempt has a real verification result.
- Improved `relevant_context` ranking: scoring and result assembly in
  `openshard/history/query.py` were reworked for better signal quality.
  Every rendering path (`openshard context`, the MCP tool, `--text`) still
  shares the exact same ranking. No embeddings, fuzzy matching, or model
  calls.
- Local stdio MCP server (`openshard.mcp.server`, `openshard mcp install
  claude`): a read-only server scoped to one repository's local history,
  exposing `recent_shards`, `get_shard`, `get_receipt`, `search_history`,
  and `relevant_context` as MCP tools. No network access, no API key.
- Local history query layer (`openshard/history/query.py`,
  `openshard/history/locate.py`, `openshard/history/views.py`): the shared
  read path behind `openshard history`, `openshard context`, `openshard
  stats`, and the MCP server's tools, all built on one privacy-bounded dict
  projection so the CLI and the MCP server can never diverge.
- Canonical Shard/Attempt/Event model: run history now has one typed
  `Event` model (`openshard/history/event.py`) instead of ad hoc dicts,
  used consistently by native OSN runs, the Claude Code import path, and
  receipts; and persistent Shard **attempt** grouping, so retries of the
  same task are tracked as attempts of one Shard rather than unrelated
  runs. This is the foundation the local history query layer,
  `relevant_context`, and the richer receipts below are built on. No
  change to `runs.jsonl`'s on-disk format for existing fields.
- Near-zero blocking Claude Code capture (PR9.5): the hooks Claude Code waits
  on no longer spawn a Python process or fold a receipt. `UserPromptSubmit`,
  `PostToolUse`, `PostToolUseFailure`, `Stop` and `SessionEnd` are installed
  as Claude Code's official **HTTP hooks**, POSTing the payload to a warm,
  loopback-only local capture service (`openshard capture serve`, started
  automatically by the `SessionStart` command hook and by `openshard setup`).
  The service's blocking path only validates, reduces the payload to the
  same privacy-safe shape as before (scrubbed task excerpt, repo-relative
  path, summarized command -- never raw prompts, transcripts or absolute
  paths), appends it to a per-session queue file with `fsync`, and returns;
  a background worker then replays the queue through the unchanged fold
  logic, so Shard records and receipts are byte-for-byte what the
  synchronous path produced, just eventually consistent (normally within a
  few hundred milliseconds). Replays are idempotent (every queued event has
  an id the session buffer remembers) and leftover queues are recovered on
  service start and on the next `SessionStart`, so a crash or kill loses no
  acknowledged event. The service exits by itself after 4 idle hours, on
  `openshard capture stop`, or on `mcp uninstall claude`; a port taken by
  another program is skipped for the next one in a small range and
  `setup`/`doctor` report and repair the hook URLs. `openshard capture
  status|start|stop|serve` are new; `doctor` gains a "Capture service" line;
  `setup` reports the service state. The command-form entrypoints
  (`openshard hooks claude`, `openshard hooks claude-status`) now forward to
  the service and fall back to in-process handling only when it cannot be
  reached (`OPENSHARD_CAPTURE_DISABLE=1` forces the old in-process path).
  Existing pre-PR9.5 hook configurations keep working and are upgraded in
  place on the next `openshard setup`. See `docs/capture-performance.md`.
- Strong local visibility (Free v0.4.0): a user never has to trust that
  OpenShard is "working in the background" -- four commands show exactly
  what it captured, offline, for the current repository:
  - `openshard last` is the polished "what just happened?" view: task,
    status, executor, model(s), duration, provider-reported tokens,
    estimated cost, changed files, checks, capture depth, evidence kinds and
    result, straight from the newest Shard receipt. When run from a
    subdirectory it says which repository root the history came from;
    `--json` gains a `repo` block (identity, folder name, relative history
    path -- never an absolute path).
  - `openshard history [--limit N] [--repo R] [--json]` lists recent Shards
    newest first: time, shard id, task, agent, the status OpenShard can
    truthfully claim (a completed Claude Code turn is shown as `Completed`,
    never as verified), a check summary, estimated cost, changed-file count,
    attempt count and partial-capture marker.
  - `openshard context "<task>" [--limit N] [--text] [--json]` exposes the
    same `relevant_context` the local MCP server gives an agent, with the
    signals that matched each Shard, its score, status/verification, retry
    history, changed files, non-Note findings and provenance (who ran it,
    how completely it was observed) -- plus a plain-English "How ranking
    works" footer generated from the scorer's own constants. `--text`
    prints the exact block an agent would receive. Retrieval quality is
    unchanged (PR10).
  - `openshard stats [--limit N] [--repo R] [--json]` gives honest counts
    derived from existing receipts: Shards/attempts/retries, agents, origin
    and capture depth, models (with an explicit `unknown` bucket),
    verification outcomes, Claude Code turn status (labelled as not being
    verification), estimated cost with the number of Shards it covers and
    the number missing it, provider-reported token totals, observed
    duration, files changed and the most-changed files. No productivity or
    efficiency scores. `stats completeness` / `stats failures` are unchanged.
  - `openshard.history.locate` resolves the history root for all of the
    above; `openshard.history.views` holds the single privacy-bounded dict
    projection shared by the MCP server and the CLI `--json` surfaces.

- Zero-friction onboarding: `openshard setup` is now the one command a new
  user needs. It detects the environment (git repository, Claude Code CLI,
  existing MCP/hook/status-line configuration, local history writability),
  configures Claude Code capture for the current repository by orchestrating
  the existing `mcp install claude` installers (MCP server, auto-capture
  hooks, status-line enrichment), and reports one of three honest
  outcomes: ready, ready with a limitation (e.g. a custom status line it
  will not replace, so model/cost/token data stays unavailable), or not
  ready with the exact next step. Safe to re-run; already-configured
  components are left byte-for-byte alone. `--yes` skips the interactive
  provider wizard, `--json` returns a machine-readable result, and
  `--agent` remains a read-only status snapshot that never writes. No API
  key, account, or network is required.
- `openshard doctor` now includes a Claude Code checklist (Repository,
  Local history, Claude Code, MCP, Auto-capture hooks, Receipt enrichment)
  with a ✓/✗ per line, the specific problem for each ✗, and a one-line
  verdict; `--json` gains a `claude_code` block. Read-only.
- `openshard mcp uninstall claude` — reverses `setup` / `mcp install
  claude`. Removes only OpenShard's own local-scope MCP entry, hook
  entries, and status line (a custom status line is never touched);
  unrelated Claude Code settings and all `.openshard/` history are left
  intact.
- Richer Claude Code receipts:
  - A task's receipt is now available as soon as its turn finishes (`Stop`
    fires) — it never has to wait for `SessionEnd`, which stays independent
    session metadata (`capture.task_status`, distinct from verification).
  - Model, cumulative session cost, and input/output/cache token counts are
    now captured from Claude Code's *status line* (`statusLine` setting) —
    the only official, local, no-network surface that reports them; no hook
    payload carries this data. `openshard mcp install claude` configures a
    status line automatically when the project has none of its own yet
    (`--no-statusline` to skip); a `openshard hooks claude-status` command is
    installed as its entrypoint. Model switches mid-session are preserved
    (never flattened to one model); cost is windowed to this Shard's session
    (baseline-subtracted, never a whole-session cumulative total dumped onto
    one receipt) and always labelled `est.` — never billing truth.
  - Task-boundary duration (first prompt → most recent completed turn, not
    whole-session time), a repo-relative `Files` list with change-type
    letters, per-tool `Activity` counts, and an `Evidence` summary line are
    now shown in the compact receipt. All are additive and gated on data
    being present, so existing receipts render unchanged.
  - Unknown model/cost/tokens still render as `Unknown` / `Not recorded` —
    never guessed from names, env vars, or user text.
- Claude Code auto capture: `openshard mcp install claude` now also installs
  OpenShard's Claude Code lifecycle hooks (`SessionStart`, `UserPromptSubmit`,
  `PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionEnd`) into the
  repository's `.claude/settings.local.json`, so normal `claude` sessions are
  recorded automatically as Shards/Receipts with canonical Events — no
  `openshard import claude` / `openshard wrap claude` step. Pass `--no-hooks`
  to configure MCP only.
- `openshard hooks claude` — the non-interactive hook entrypoint Claude Code
  invokes (hook JSON on stdin, silent stdout, always exit 0).
- Untracked new files are now reported by the hook capture (`git ls-files
  --others`); work committed during a session is diffed against the HEAD
  snapshotted at session start.

### Fixed

- `openshard last` (and `stats completeness` / `stats failures`) read
  `.openshard/runs.jsonl` relative to the current directory, so running
  them from a subdirectory of a repository reported "No run history found"
  even though Claude Code hooks had recorded Shards at the repository root.
  They now resolve the repository's history root from any subdirectory
  (`repo`, `repo/subdir`, `repo/subdir/deeper` all read the same file),
  stop at the nearest `.git` so a nested repository never reads its
  parent's history, and never reach a sibling repository. Non-git
  directories keep the previous cwd behaviour.

### Changed

- Claude Code capture performance hardening: `Stop` (fires every turn) and
  `SessionEnd` are synchronous hooks, and the status line is inherently
  synchronous, so their latency was fully visible to the user. The
  `openshard` console script now fast-paths `hooks claude` /
  `hooks claude-status` around the full CLI's import graph (run pipeline,
  provider clients, evals, planning), and the status-line handler no longer
  performs a git diff / git-identity lookup / `runs.jsonl` rewrite on every
  ping — it only updates the lightweight staging buffer and lets the next
  real fold boundary (a throttled tool-hook snapshot, `Stop`, or
  `SessionEnd`) pick up model/cost/token values. Lock waits on the hook/
  status-line path are now bounded (fails open, Claude Code is never
  blocked) rather than unbounded. See `docs/capture-performance.md` for
  what was measured. No schema or behavior change to `runs.jsonl` or the
  hook command Claude Code invokes.

## 0.3.0 - 2026-06-06

First-class Claude Code session receipt import, skills list command, and tooling hardening.

### Added

- `openshard import claude` — import Claude Code session receipts directly into OpenShard history (PR #262)
- `openshard skills list` command to enumerate available skills (#257)

### Changed

- Import sorting and pyupgrade rules added to Ruff linting (#261)
- README revised for clarity on OpenShard's role

### Fixed

- `--from` flag renamed to `--notes` in `openshard import claude` for clearer UX
- Sandbox tests failing in environments with git commit signing (#259)
- Stale `claude-opus-4.6` reference in `config.yml` (#258)
- Added `pipx install` prerequisite to install docs and README (#260)

### Docs

- Updated `docs/what-is-a-shard.md`

## 0.2.0 - 2026-06-05

The proof, receipts, safety, and local history hardening release.

### Added

- Shard Proof Contract — a formal, consistent shape for run proof
- `openshard proof last` to inspect the latest run's proof
- Shard quality summary in `openshard last --json`, plus a compact
  `Proof: <status>` line in `openshard last`
- Content hash verification for Shards
- Run trust score, completeness stats, and failure taxonomy stats
- Generate an eval case from a failed Shard
- Best-effort pre-send secret scanning before provider calls
- CI check mode (pass / warn / fail / skip) with deterministic exit, plus
  GitHub Actions PR receipt outputs
- Machine-readable proof and run timeline output
- Repo map with caching and repo-aware plan mode
- First-run onboarding commands
- Model registry metadata and model lifecycle tags

### Changed

- Routing truth made clearer in proof output (routing behavior unchanged)
- License changed from MIT to Apache-2.0
- Runtime gate decision ordering unified across execution path

### Fixed

- Safer JSONL history writes (write locking)
- Model registry drift corrected to a single source of truth
- CI check GitHub Actions output test isolation
- Repo map path sanitisation on Linux

### Docs

- Added "What is a Shard?" explainer (`docs/what-is-a-shard.md`)

## 0.1.2 - 2026-06-01

- Published OpenShard 0.1.2 to PyPI
- Confirmed clean `pipx install openshard` path
- Fixed package config defaults for clean out-of-the-box install
- Improved package/source command parity
- Included proof-flow commands through the installed package
