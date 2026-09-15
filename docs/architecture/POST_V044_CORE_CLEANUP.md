# Post-v0.4.4 core cleanup: evidence-backed recommendations

Scope for the next bounded cleanup phase. v0.4.4 refactored only where
the four integrity fixes needed it; this lists what the audit found and
deliberately left alone. Line counts are from the v0.4.4 branch. Nothing
here should be deleted because it is large; each item names its evidence.

## 1. Genuinely dead or unreferenced

| Item | Evidence | Recommendation |
|---|---|---|
| `native/agent_loop_dry_run.py`, `native/agent_loop_tool_runner.py` | No import anywhere in `openshard/` (grep for the module name finds only the file itself). Tests may import them directly. | Confirm with the test suite, then either wire them or remove them. Do not remove if a test exercises behaviour that nothing else covers. |
| Display-time review-task risk floor | Removed in v0.4.4 (`shard_contract.build_shard_receipt`). The run-time floor in the pipeline still exists. | Decide whether the pipeline should *store* the raised risk with its basis (`risk_basis: review_task_floor`) so the receipt can show it as a labelled fact. |

## 2. Duplicated

| Concept | Copies | Recommendation |
|---|---|---|
| `_now()` UTC-timestamp helper | 4 module-private copies (adapters, history, telemetry) | One `openshard/util/time.py` helper; mechanical. |
| Sanitisation | `safety/sanitize.py` (canonical) plus module-local `_sanitize_task` in `wrap_exec.py` and `claude_code_import.py`, `_sanitize_meta` in `analysis/repo_map.py`, `_sanitize_event_metadata` in `history/event.py`, `_sanitize_file_paths` in `history/interactions.py` | Route the module-local task/metadata scrubbers through `safety/sanitize.py`; keep path vs text distinction (`sanitize_path` exists for a documented false-positive reason). |
| Hook-entry merge logic | `claude_hooks_install.merge_openshard_hooks` is reused by Codex/Cursor installers via callbacks — good — but each installer re-implements `is_ours`, `_hook_entry`, exclude handling with small differences | Extract a tiny `hook_installer` base (spec → entry, is_ours, exclude) and keep agent files as data. |
| Git subprocess helpers | `_run_git` in `analysis/repo_map.py`, `_parse_git_changed_files`/`_list_untracked_files` in `claude_code_import.py`, `_run_git`/`_blob_ids` in `claude_hooks.py` (v0.4.4), `repo_identity.py` | One `git_observe.py` with timeout, `_NO_WINDOW_KW`, and repo-relative sanitisation applied once. |
| Test helpers | `_git`, `_make_repo`, `_payload`, `_stable`/`_stable_view` reimplemented in 5+ test modules; v0.4.4 tests import them from `test_claude_capture_service` | Move shared fixtures to `tests/_capture_fixtures.py` (or `conftest.py`). |

## 3. Modules to split

| Module | Lines | Suggested seams |
|---|---|---|
| `cli/main.py` | 6541 | Command groups already exist logically: `receipts` (last/history/context/stats), `capture`, `mcp`, `setup/doctor`, `trust/proof`, `run/native`, `pr/reflect`. One Click group per file under `cli/commands/`; `main.py` keeps registration only. Start with `capture` + `doctor` (trust-critical, touched by v0.4.4). |
| `run/pipeline.py` | 2754 (mypy-excluded, 27 broad excepts) | Extract `_log_run` and record building (already partly in `_pipeline_helpers.py`) so the record writer is typed and testable; lift the mypy exclusion afterwards. |
| `adapters/claude_hooks.py` | 2641 | Three files: `hook_payloads.py` (HookPayload/StatusPayload/Reduced + translators' shared vocabulary), `session_buffer.py` (buffer lifecycle, locks, rebuild), `hook_fold.py` (`_apply`, `build_hook_entry`, attribution). Attribution helpers added in v0.4.4 (`_snapshot_baseline`, `_classify_changed_files`) are a natural first extraction. |
| `history/shard_contract.py` | 1988 | `receipt_model.py` (dataclasses), `receipt_build.py` (`build_shard_receipt`), `receipt_render.py` (compact/full). |
| `cli/run_output.py` | 2572 | Belongs with the native pipeline UI; move under `cli/native/`. |
| `native/context.py` | 3742 | Native-only; out of receipt scope. Split by concern later. |

## 4. Overlapping concepts

| Concepts | Files | Observation |
|---|---|---|
| Trust Score, Shard quality, Proof contract, completeness (field presence), proof signals, CI policy check | `trust_score.py` (339), `shard_quality.py` (135), `proof_contract.py` (701), `completeness.py` (212), `proof_signals.py` (63), `ci/policy_check.py` (165) | Six ways to say "how good is this record". v0.4.4 added a seventh, deliberately narrow one (`capture_completeness`: *known loss only*). Recommendation: keep `capture_completeness` and the proof contract as facts; treat Trust Score and field-presence completeness as diagnostics behind `openshard trust`/`stats`; document the boundary in one place. |
| `capture_depth` (shard.py) vs `capture.completeness.status` | `history/shard.py`, `history/capture_completeness.py` | Complementary by design (depth = how much could ever be seen; completeness = what was lost). Keep both, but render them on one `Capture` line as v0.4.4 does. |
| `files_source` vs `changes.baseline.source` vs `files_detail[].attribution` | hook records | Three provenance labels for files. `files_source` predates attribution; keep for compatibility, stop adding to it. |

## 5. Move behind an advanced/native boundary

* `openshard trust`, `openshard proof`, `openshard reflect`, evals, packs,
  routing/model registry commands and the TUI: keep, but group them under
  the existing "Advanced" help section and out of `docs/architecture.md`'s
  primary path (done in v0.4.4 for the doc; the CLI grouping exists since
  v0.4.3).
* `history/completeness.py` (field-presence heuristic) should be renamed
  to avoid confusion with `capture_completeness.py` (e.g.
  `receipt_field_coverage.py`).

## 6. Broad `except Exception`

Counts: `run/pipeline.py` 27, `adapters/claude_hooks.py` 21,
`run/_pipeline_helpers.py` 16, `history/event.py` 15, `cli/main.py` 11.
In the capture path most are deliberate "observational code never
propagates into the agent" guards and should stay, but each should (a)
record an `error.occurred` telemetry category or a stats counter and (b)
be narrowed where the failure type is known (`OSError`, `ValueError`,
`LockTimeoutError`). v0.4.4 added no new broad excepts on the request
path beyond the existing pattern.

## 7. Stale comments and history markers

73 comment lines across 22 modules reference PR numbers or migration
labels (`PR9.5`, `PR12`, `Migration 3`, `Demo v1`). They are accurate but
opaque to new readers. Replace with the concept name (e.g. "capture
service", "agent-neutral fold", "canonical Events") in one mechanical
pass; keep `CHANGELOG.md` as the history.

## 8. Do not remove

* OSN, routing, evals, workflow packs, TUI: working, tested, and out of
  scope for receipt integrity. Grouping and documentation, not deletion.
* `shard_id` minting and grouping: every consumer keys on it.
* `history/completeness.py`: `openshard stats` uses it; rename, don't drop.
* The in-process fallback fold in every hook entrypoint: it is what keeps
  capture working when the service cannot start.
* Trust Score: diagnostic value for native runs; keep behind `openshard trust`.

## 9. Known limitations carried into the cleanup

* `_parse_git_changed_files` historically capped at 20 files; the hook
  fold now asks for 200 and caps *reported* files at 50 and excluded at
  50 (`changes.files_truncated`). Import/wrap adapters still use 20.
* Working-tree baseline is taken at the first observed hook; agents
  whose sessions start without a start hook (Cursor background agents)
  get a later baseline. Documented in `docs/agent-capture.md`.
* Other-session attribution consults live buffers only; an ended
  sibling session's files become `git_observed`, never `agent_reported`.
