# Changelog

All notable changes to OpenShard are documented here.

## Unreleased

### Added

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
