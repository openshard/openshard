# Changelog

All notable changes to OpenShard are documented here.

## Unreleased

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
