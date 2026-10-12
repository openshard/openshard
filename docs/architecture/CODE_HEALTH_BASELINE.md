# Code health baseline and refactoring plan

Measured on `main` at `fd70787a` (the v0.4.15 commit) on 2026-10-12 with
`python scripts/code_health.py`, which uses only the standard library so every
machine and CI agree. `tests/test_code_health_ratchet.py` embeds these numbers
as budgets: a unit that grows past its budget fails, and a unit that shrinks
well below it fails until the budget is lowered. Nothing in this document is
proposed because a file is "big"; each row names what is wrong with the
structure, what belongs together, what should move, what must stay stable,
and which tests protect the change.

Companion documents: `POST_V044_CORE_CLEANUP.md` (duplication, stale markers
and typing, re-measured 2026-10-11) and `tests/test_architecture_boundaries.py`
(package dependency rules). This file owns module size, function size,
complexity and broad exception handling.

## Totals

| Measure | Value |
|---|---|
| Modules in `openshard/` | 328 (108,954 lines) |
| Test modules | 334 (133,519 lines) |
| Modules over 1,500 lines | 9 |
| Functions of 150+ lines | 30 (71 of 100+) |
| Functions with cyclomatic complexity over 25 | 46 (McCabe count; ruff `C901` reports 20 over 25 because it attributes nested closures differently) |
| Broad `except Exception` handlers | 432 across 108 modules; 18 modules have more than 5 |
| Architecture boundary exceptions | 3 (`run.pipeline -> cli.run_output`, `history.jsonl_store -> learning.worker`, `sync.policies -> osn.budget`) |
| Module-level import cycles | 0 |
| mypy exclusions | 1 (`run/pipeline.py`) |

## Budgets

| Signal | General ceiling | Why |
|---|---|---|
| Module length | 1,500 lines | Readable in one sitting; the nine modules above it each hold several concerns (below). |
| Function length | 150 lines | Fits one screen; above it a reviewer cannot hold the branches in mind. |
| Cyclomatic complexity | 25 | Beyond this a function cannot be tested path by path without fixtures for every combination. |
| Broad handlers per module | 5 | Enough for the deliberate "observation never breaks the agent" guards, too few to hide defects. |

The ratchet does not force any refactor; it stops growth. The intended way to
add a command, a branch or a handler to a listed unit is to extract something
from it first.

## The nine modules over the ceiling

Importer counts are modules in `openshard/` (and test modules) that import
the module, measured with `grep`. They bound how much of the public surface
each split must keep.

### `cli/main.py` (8,567 lines; 3 importers, 149 test modules)

What is wrong: one module registers 162 Click commands in 30 groups and also
holds their implementations, the log renderer (`_render_log_entry`, 433 lines,
complexity 110), `doctor` (198 lines) and the receipts/verify/report commands.
Nothing else imports it except the entrypoint, so its size is a reading and
testing cost, not a coupling one, but every command change touches an
8,500-line file and the fast-path entrypoint has to special-case it to keep
hook latency down.

What belongs together: each Click group with the helpers only it uses.
`main.py` keeps the root group, shared options and registration.

What should move, in order of value:

1. `_render_log_entry` and the `last` / `history` / `context` / `stats`
   rendering into `cli/receipts_cmd.py` (the `receipts` surface is the one
   users see most and the one Receipt changes touch).
2. `capture` and `doctor` into `cli/capture_cmd.py` (trust-critical, touched
   by every capture fix).
3. `mcp`, `hooks`, `grok-bot`, `import`, `wrap` into one `cli/agents_cmd.py`
   or one file each (`osn_cmd.py`, `learn_cmd.py`, `workflow_cmd.py` and
   `usage_cmd.py` already follow this shape).
4. `trust`, `proof`, `reflect`, `demo`, `packs`, `eval` into
   `cli/advanced_cmd.py` (already grouped as "Advanced" in help).

What must stay stable: the console script target (`openshard.cli.entrypoint`
and `openshard.cli.main:cli`), every command path and option (the 149 test
modules invoke them through `CliRunner`), and the exact argv the entrypoint
fast path recognises (`hooks claude`, `hooks claude-status`).

Regression tests: `tests/test_cli_*.py` (per command), `tests/test_cli_entrypoint.py`
for the fast path, `tests/test_first_run_ux.py` for `doctor`. Each extraction
is one PR that moves one group and its tests' imports; no behaviour change.

### `adapters/claude_hooks.py` (4,918 lines; 18 importers, 35 test modules)

What is wrong: the module's own section headers show seven concerns in one
file: payload parsing, status-line parsing, transcript usage, repository
resolution and path privacy, the reduced payload, the per-session staging
buffer (including locks and rebuild), record creation and fold, session git
outcome, event handlers and status handling. `_apply` (367 lines, complexity
93) and `build_hook_entry` (398 lines, complexity 84) are the fold and the
record builder, and both are where the 28 broad handlers concentrate. The
other agent adapters (`cursor`, `codex`, `opencode`, `hermes`, `grok_bot`)
import pieces of it, so it is also the de-facto shared capture library.

What belongs together: (a) payload vocabulary (`HookPayload`, `StatusPayload`,
the reduced payload, parsing and sanitisation); (b) the staging buffer
(`buffer_path`, `_read_buffer`, `_write_buffer`, `_load_buffer_light`,
`_buffer_from_entry`, `sweep_stale_buffers`, the lock timeouts); (c) the fold
(`_fold`, `_apply`, `build_hook_entry`, attribution, session git outcome);
(d) the entry points (`handle_claude_hook`, `handle_claude_status`,
`apply_reduced_hook`, `apply_status_payload`, `run_*_from_stream`).

What should move: (a) to `adapters/hook_payloads.py`, (b) to
`adapters/session_buffer.py`, (c) to `adapters/hook_fold.py`; (d) stays in
`claude_hooks.py` and re-exports the names the other adapters and the capture
service import today, so no importer changes in the first PR.

What must stay stable: every name the 18 importers use (list them with
`grep -rn "claude_hooks import" openshard`), the buffer file format under
`.openshard/claude_sessions/`, the `runs.jsonl` entry shape, the
`HookOutcome` contract, and the status-line latency (the fast path imports
this module; a split must not add imports to the status path, which
`test_status_line_never_spawns_a_subprocess_in_steady_state` and the budget
test check).

Regression tests: `tests/test_claude_hooks.py` (2,100+ lines, the fold and
buffer behaviour), `tests/test_cli_claude_hooks.py`, `tests/test_capture_*.py`,
`tests/test_connected_capture.py`, `tests/test_claude_capture_service.py`;
`tests/capture_fixtures.py` provides the shared repository fixtures.

### `history/shard_contract.py` (3,096 lines; 38 importers, 82 test modules)

What is wrong: the Receipt model (`ShardReceipt`, `ShardFinding`,
`FileEvidence`, `ExecutionSpan`, `EvidenceCapsule`), the builder
(`build_shard_receipt`, 606 lines, complexity 136) and the renderers
(`render_full_shard_receipt`, 496 lines, complexity 170;
`render_compact_shard_receipt`; the OSN render helpers) are one module. The
builder is the single place every evidence field is interpreted for display
and for sync, so its complexity is a Receipt-correctness risk: a change for
one agent's evidence passes through branches for every other agent.

What belongs together: model; build (evidence interpretation, one helper per
evidence family: capture, verification, usage, changes, OSN); render.

What should move: dataclasses and display-label helpers to
`history/receipt_model.py`; `build_shard_receipt` and its `_*` helpers to
`history/receipt_build.py`, split by evidence family; the render functions
to `history/receipt_render.py`. `shard_contract.py` keeps the public names as
re-exports.

What must stay stable: `build_shard_receipt(entry)` and
`history.views.receipt_to_dict(receipt, extended=True)` (the sync projection the Platform
contract is written against: `packages/contracts/src/receipt-sync.ts`), the
rendered text (snapshot-tested), and `ShardReceipt` field names.

Regression tests: `tests/test_shard_contract.py`, `tests/test_receipt_*.py`,
`tests/test_platform_sync.py` (projection), `tests/test_routing_truth*.py`,
`tests/test_verification_*.py`. The Platform's
`core-capture-integration.test.ts` proves the projection end to end.

### `run/pipeline.py` (2,827 lines; 2 importers, 10 test modules)

What is wrong: `RunPipeline.run` is one 2,473-line method with cyclomatic
complexity 482 and 27 broad handlers; the module is excluded from mypy. It is
the native run path (planning, execution, review, verification, record), so
it is not on the capture path, but it is untyped and untestable in parts.

What belongs together: each stage of the run (plan, execute, review, verify,
record) with its own error policy.

What should move: `_log_run` and record building are already partly in
`_pipeline_helpers.py` (343 lines, complexity 60); finish that extraction
into a typed `run/record_writer.py` first, because it is what writes the
native Receipt. Then lift the mypy exclusion for the helpers, then extract
stages from `run` one at a time behind the same `RunResult`.

What must stay stable: `RunPipeline.run()`'s signature and `RunResult`, the
`runs.jsonl` entry it writes, and the `cli.run_output` rendering calls (the
boundary exception `run.pipeline -> cli.run_output` disappears when the
pipeline returns display data instead of printing).

Regression tests: `tests/test_run_*.py`, `tests/test_native_receipt.py`,
`tests/test_native_executor.py`, `tests/test_cli_native_loop.py`,
`tests/test_pr13_*.py` (benchmark harness). The record writer needs a
contract test of its own against `history/shard_schema.coerce_shard_entry`.

### `cli/run_output.py` (2,582 lines; 3 importers, 33 test modules)

What is wrong: `_render_native_demo_block` is 1,046 lines with complexity
300, one function rendering every native-run block. The module mixes the
post-run summary used by `openshard run` with native/OSN demo rendering.

What should move: `_render_native_demo_block` into one function per block
(tier dispatch, candidate scoring, provenance, checkpoints, failure memory
already have separate renderers; the demo block duplicates their shapes),
under `cli/native/`. `render_post_run`, `render_run_timeline` and the review
memos stay as the `run` command's output.

What must stay stable: rendered text (snapshot tests), and the functions
`pipeline.py` calls until that boundary exception is removed.

Regression tests: `tests/test_run_output.py` and the 25 `tests/test_native_*.py`
modules snapshot the blocks.

### `cli/osn_cmd.py` (2,388), `native/context.py` (3,742), `native/executor.py` (1,683)

Native and OSN only; out of the Receipt path. `osn_run` (579 lines,
complexity 121) and `_resolve_workers` (191) should become an options
dataclass plus a runner; `native/context.py` holds 34 `build_*` functions
that are already independent and can move into `native/context/` by family
(plan ledger, candidate agents, tier dispatch, provenance, scoring) with
`context.py` re-exporting. Lower priority than the four modules above; ratchet
only.

### `adapters/claude_capture_service.py` (1,557)

`serve` (177 lines) is the HTTP loop plus flusher and idle-watch threads.
Extracting the request handler and the idle watch into named classes would
make the service testable without a socket. Lower priority; the capture
service has its own integration tests (`tests/test_claude_capture_service.py`).

## Broad exception handlers

432 handlers, 18 modules above five. Most sit on the capture path where the
contract is "observation never propagates into the agent" and are correct.
The ratchet stops the count growing; each PR that touches one of these
modules should narrow the handlers it passes (`OSError`, `ValueError`,
`LockTimeoutError`, `json.JSONDecodeError`) and record a telemetry
`error.occurred` category where it swallows. The modules where narrowing is
most valuable because the handler can hide a Receipt defect:
`adapters/claude_hooks.py` (28), `adapters/claude_capture_client.py` (19),
`run/_pipeline_helpers.py` (17), `history/event.py` (15), `sync/client.py` (13).

## Order of work

1. Ratchet in place (this change) so none of the above grows while it is being
   split.
2. `history/shard_contract.py` build/render split: highest Receipt-correctness
   value, 38 importers protected by re-exports.
3. `adapters/claude_hooks.py` buffer and fold extraction: the capture path's
   defect surface, with the latency tests as the guard.
4. `cli/main.py` command groups, one group per PR, starting with receipts and
   capture/doctor.
5. `run/pipeline.py` record writer, then the mypy exclusion.
6. `cli/run_output.py` demo block, native/OSN modules, capture service.

Re-measure with `python scripts/code_health.py` after each step and lower the
budgets in `tests/test_code_health_ratchet.py`; the test tells you which.
