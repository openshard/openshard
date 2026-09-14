# OpenShard v0.4.4 — Receipt Integrity Hardening: implementation report

Branch: `claude/v0.4.4-receipt-integrity-noucoo` (the harness-designated
working branch; the suggested name `feat/v0.4.4-receipt-integrity` was not
used because the session's push target is fixed). Base: `main` at
`25de0b6102af6c62817da79f6658dad8b58acd66` (the v0.4.3 release commit;
verified unmoved at start). Nothing was merged, tagged, published or
deployed. The private Platform repository was not touched.

## Commits (small, logical)

| # | Commit | Content |
|---|---|---|
| 1 | `docs: v0.4.4 receipt integrity audit (Phase 0)` | `docs/architecture/V044_RECEIPT_INTEGRITY_AUDIT.md` |
| 2 | `tests: reproduce the four v0.4.4 receipt-integrity problems` | Red-by-design regression tests for attribution, authentication, identity, corrupt evidence |
| 3 | `history: additive global receipt_id minted at record creation` | `receipt_identity.py`; minting in every writer; renderers/JSON/MCP/search |
| 4 | `capture: authenticate the local capture channel` | `capture_auth.py`; service, client, installers, plugin, CLI, doctor, telemetry counters |
| 5 | `capture: never forget corrupt queued evidence; make completeness explicit` | `capture_completeness.py`; quarantine; `apply_capture_loss`; receipt/JSON |
| 6 | `capture: attribute changed files instead of claiming the whole git diff` | baseline snapshot, classification, `changes` block, receipt rows |
| 7 | `receipt: say only what the evidence supports` | status wording, risk floor removed, Integrity row, test isolation |
| 8 | `docs: v0.4.4 architecture, trust boundary, attribution and cleanup plan` | architecture, SECURITY, agent-capture, README, CONTRIBUTING, CHANGELOG, version |
| 9 | `capture: log refused requests (throttled); v0.4.4 reports` | rejection logging; this report and the review checklist |

## Exact changes

### Global receipt identity (Phase 2)
* New `openshard/history/receipt_identity.py`: `receipt_id = "rcpt_" + uuid4().hex`; `is_receipt_id`, `stored_receipt_id` (read-only), `ensure_receipt_id` (writers only).
* Minted at creation in: hook fold (`claude_hooks._ensure_record`, carried across folds and buffer rebuilds), `wrap_exec`, `claude_code_import`, native `_log_run`.
* `ShardReceipt.receipt_id`; `Receipt ID` row in compact and full receipts; `receipt_to_dict`, `last --json`, `history --json` carry it; `get_receipt` accepts a `receipt_id` in place of a `shard_id`; history search matches it.
* `shard_id` format, minting and every consumer unchanged. Old records: `receipt_id = None`, never back-filled.
* No `task_id`/`work_id`: documented in `receipt_identity.py`, `docs/architecture.md` and `docs/agent-capture.md` as an unsolved, separate problem.

### Capture authentication (Phase 3)
* New `openshard/adapters/capture_auth.py`: per-user token (`<OPENSHARD_HOME>/capture-token`, 64 hex, 0600, `O_EXCL` race-safe creation, atomic rotation); repository capability `r1.` + HMAC-SHA256(token, normalised root); constant-time `verify_presented`; browser-header detection.
* Service: every `POST` needs a plausible credential before the body is parsed (401, `stats.rejected`); browser headers 403; repository capability verified against the resolved root inside `record_hook`/`record_status` via an `authorize` callback (nothing written on refusal); `/shutdown` requires the token itself; `/health` unchanged and unauthenticated; refusals logged with path and reason only, throttled.
* Client: `post_hook`/`post_status`/`request_shutdown` present the token (created on first use); `SessionStart` self-heals Claude hook entries lacking a valid capability.
* Claude installer: HTTP hook entries carry the repository capability; `capability_state()`; refuses to embed when git tracks `.claude/settings.local.json` (warning). `claude_setup.detect_claude_integration` reports `hooks_auth_state` and flags upgrade; `doctor` explains.
* OpenCode plugin v3 reads the token file at delivery time (`OPENSHARD_HOME` honoured).
* CLI: `openshard capture rotate-token`; `capture status` shows refused/quarantined counts.
* Telemetry: `capture.service` gains bounded ints `rejected`, `corrupt_lines`; no free text, token cannot be sent.

### Evidence loss and completeness (Phase 4)
* New `openshard/history/capture_completeness.py`: `full`/`partial`/`incomplete`/`unknown`; reasons `corrupt_queued_event`, `dropped_hook_events`, `session_end_not_observed`, `integration_limitation`; `derive_capture_completeness` (stored block wins; otherwise derived and labelled).
* Service replay returns a structured result; undecodable/structurally unusable lines are quarantined (bounded 4 KB copy, header names only the queue file) under `.openshard/claude_sessions/quarantine/`, counted, and reported to `claude_hooks.apply_capture_loss()`; valid neighbours still applied; transient errors keep the retry path.
* Hook records store `capture.completeness`; stale sweep records `session_end_not_observed`.
* Receipts: `Capture  Incomplete — …` above the existing partial line; `receipt_to_dict` and `last --json` carry the block.

### Change attribution (Phase 5)
* Baseline at first observed hook (`git status --porcelain -z --untracked-files=all` + `git hash-object --stdin-paths`, bounded 500); the service takes it when `SessionStart` is received and carries it on the queue line so replay lag cannot fold the agent's first edits into it.
* Classification per diff path: `agent_reported`, `git_observed` (with `pre_existing`/`agent_attempted` flags), `pre_existing` (excluded), `other_session` (excluded; from sibling live buffers).
* Record: `files_detail[].attribution`, `changes` block (counts, baseline summary and paths for rebuilds, `files_truncated`); counts exclude pre-existing/other-session; `file.changed` events keep `evidence=git_observed` and gain `metadata.attribution`.
* Receipt: `Changed  N files (a agent-reported; g git-observed, actor not established)`, `Excluded  n pre-existing …` / `Excluded  n other-session …`, per-file tags; full receipt lists excluded files and the baseline.

### Receipt semantics (Phase 6)
* `Turn completed (unverified)` / `Session ended (no turn observed)`; stats bucket old and new wording together.
* Display-time review-task risk floor removed; risk shown as recorded.
* `Integrity  Matches (content hash)` / `Mismatch (content hash)` / `Not recorded`; no "signature" wording anywhere.
* Trust Score untouched, still behind `openshard trust` and `--json`.
* No owner/requester/executor/approver fields added or inferred.

### Test isolation (Phase 9)
* `tests/conftest.py` repoints `DEFAULT_PORT` in both the client and service modules at a free ephemeral port for every test. Confirmed problem: `ensure_service`, `_candidate_ports` and the no-state-file fallback previously probed the real 47811 and could observe or stop a developer's live service. `tests/test_v044_test_isolation.py` pins the fix.

## Files changed (50)

Source: `openshard/adapters/{capture_auth.py (new), claude_capture_client.py, claude_capture_service.py, claude_code_import.py, claude_hooks.py, claude_hooks_install.py, claude_setup.py, opencode_plugin_install.py, wrap_exec.py}`, `openshard/history/{capture_completeness.py (new), receipt_identity.py (new), query.py, shard_contract.py, stats.py, views.py}`, `openshard/cli/{main.py, visibility.py}`, `openshard/run/_pipeline_helpers.py`, `openshard/telemetry/schema.py`, `pyproject.toml`.

Docs: `docs/architecture.md (new)`, `docs/architecture/{V044_RECEIPT_INTEGRITY_AUDIT.md, POST_V044_CORE_CLEANUP.md} (new)`, `SECURITY.md`, `CONTRIBUTING.md`, `README.md`, `CHANGELOG.md`, `docs/{agent-capture.md, telemetry.md, cli-reference.md, release-checklist.md}`.

Tests: new `tests/test_v044_{change_attribution, receipt_identity, capture_auth, evidence_loss, receipt_semantics, test_isolation}.py`; updated `conftest.py`, `test_claude_capture_service.py`, `test_claude_hooks.py`, `test_cli_visibility.py`, `test_codex_capture.py`, `test_cursor_capture.py`, `test_last_rendering.py`, `test_mcp_install.py`, `test_mcp_server.py`, `test_native_receipt.py`, `test_opencode_capture.py`, `test_shard_contract.py`, `test_telemetry_cli.py`.

## Tests added

| File | Covers |
|---|---|
| `test_v044_change_attribution.py` (13) | clean repo; dirty tracked file before session; untracked file before session; agent-reported edit; git-only change; pre-existing file changed again; agent edit of pre-existing file; deleted pre-existing; baseline timing; two concurrent sessions; second session after first's uncommitted work; old record compatibility |
| `test_v044_receipt_identity.py` (9) | id format and uniqueness (10k); 24 concurrent sessions in one repo never collide; same history position in two repos differs; stability across folds and buffer rebuild; rendering; old records stay `None` |
| `test_v044_capture_auth.py` (17) | token store (random, 0600, idempotent, per-home, rotation); repo capability scoping; unauthenticated/malformed/wrong-repo refusal records nothing; authenticated success; status endpoint; browser headers; health exposes no credential; shutdown not authorisable from health or a repo capability; token never in telemetry/log/state/CLI; agents fail-open |
| `test_v044_evidence_loss.py` (12) | corrupt line quarantined, counted, record incomplete, neighbours recovered; structural invalidity; fully corrupt queue; bounded quarantine without absolute paths; loss recorded once work arrives; transient `PermissionError` and replay `error` keep retry, never quarantine; completeness model |
| `test_v044_receipt_semantics.py` (12) | status wording; risk as recorded; integrity wording (never "sign"); identity rows; no fabricated owner fields; capture rows |
| `test_v044_test_isolation.py` (4) | default port isolated; resolve/candidate ports; `ensure_service` never sees a foreign service |

Updated existing tests were changed only where they pinned the behaviour this release deliberately replaces: the unauthenticated POST contract, the exact `files_detail` shape, the `Completed` wording, the review-task risk floor, the MCP `get_receipt` key set (additive keys), the telemetry `capture.service` property set (additive keys), and one mocked-git test whose blanket `rc=0` now means "tracked" to the installer.

## Test results

Baseline before edits: 8834 passed, 3 skipped; ruff and mypy clean.

After all functional changes (this branch, Linux, Python 3.11,
`pip install -e ".[dev]"` as CI): 8900 passed, 3 skipped, 883 subtests
passed (303 s). Final confirmation run on the pushed branch head (after the
receipt-label and refused-request-logging changes): 8900 passed, 3 skipped,
883 subtests passed (302 s). ruff: clean. mypy: clean (191 files).

Windows and Python 3.12 were not run here (no Windows runner in this
environment); CI runs both. Windows-specific paths touched: token file
`chmod` is skipped on win32 (documented), `normalise_root` uses `normcase`.

## Compatibility impact

* `runs.jsonl`: additive fields only (`receipt_id`, `changes`, `files_detail[].attribution/pre_existing/agent_attempted`, `capture.completeness`). Old records render unchanged; no history rewritten.
* `files_changed`/`files_created|updated|deleted` on **new** hook records exclude pre-existing and other-session changes (intended).
* `--json` / MCP `get_receipt`: additive keys (`receipt_id`, `capture_completeness`, `integrity`, `changes`, `files_excluded`, `files[].attribution`); `last --json` additionally gains `shard_id`, `receipt_id`, `capture_completeness`, `changes`, `files_detail`.
* Wording changes visible to humans: `Completed` → `Turn completed (unverified)`; `Ended (no turn observed)` → `Session ended (no turn observed)`; review-task risk no longer coerced; new `Integrity`, `Receipt ID`, `Excluded` rows.
* CLI command names unchanged; one new subcommand (`capture rotate-token`).
* Queue-line and buffer formats: additive (`baseline`, `capture_losses`); 0.4.3 queues replay on 0.4.4.
* Telemetry: two bounded integer properties added; privacy guarantees unchanged.

## Migration behaviour

* First run of the 0.4.4 service or any hook client creates `~/.openshard/capture-token`.
* Claude Code hooks installed by ≤0.4.3 carry no credential and are refused (401, counted) by the 0.4.4 service. `openshard setup` rewrites them (`updated`); the `SessionStart` command hook of 0.4.4 also self-heals them, taking effect from the next Claude Code session (hooks are snapshotted per session). Meanwhile the in-process fallback fold still records the session. `doctor` reports the condition explicitly. Verified end to end in the smoke below.
* Codex/Cursor command hooks and the OpenCode plugin need no config change (they read the token file); OpenCode's plugin file is rewritten by `setup`/`capture install opencode` (version 3) so it sends the header.
* A user whose `.claude/settings.local.json` is tracked by git gets hooks without a credential plus a warning; capture stays on the fallback path until the file is untracked and setup re-run.

## Real-agent validation performed vs simulated

| Integration | Live | What was done |
|---|---|---|
| Claude Code 2.1.270 (logged in, this container) | **Yes** | Temp repo with a dirty tracked file and an untracked file; `openshard setup --json` (installed MCP, HTTP hooks with capability, status line; service started); `claude -p` created `hello.py` with `--allowedTools Write`. Result: 5 events queued and folded, 0 refused; receipt shows `Receipt ID`, `Capture partial`, `Turn completed (unverified)`, `Changed 1 file (1 agent-reported)`, `Excluded 2 pre-existing`, `Integrity Matches (content hash)`; JSON carries `changes`, attribution, completeness; token absent from JSON, log and state file. Then: stripped the capability from the hooks file → forged POST refused 401, `doctor` named the problem, `openshard hooks claude` (SessionStart) restored the 5 headers, `doctor` green again. `Model Unknown` is expected in `-p` mode (no status line). |
| Codex | No (CLI not installed here) | Translator, installer and service path covered by the existing and new tests with fixtures only. |
| Cursor | No (CLI not installed here) | Same: fixtures only, including the fail-open decision reply. |
| OpenCode | No (CLI not installed here) | Plugin source change is covered by the existing node-executed plugin tests only if node is present in CI; the header addition itself was verified by reading the rendered source, not by a live OpenCode session. |

One observation: during the live smoke the service counted 2 refused requests where 1 was expected (the forged POST). A deterministic re-run of the same command sequence (doctor, SessionStart hook, capture status) produced 0 refusals, so the extra one did not come from OpenShard's own clients; the most likely source is a late Claude Code hook delivery after the credential had been stripped. Refusals are now logged with path and reason (throttled) so this can be diagnosed next time.

## Unresolved risks

1. **Claude Code header interpolation was not needed** (a static header is written), so the one documented-but-unverified mechanism (`$VAR` in headers from Claude Code's process env) is not relied on. The static-header approach was verified live.
2. **Repository capability in a repo-local file.** Mitigated by scoping (events for that repo only, never shutdown), git exclusion, tracked-file refusal and rotation. A user who copies the file elsewhere still leaks a per-repo capability.
3. **In-flight sessions across upgrade** lose HTTP-hook evidence until the next session (fallback fold covers command hooks only). Documented.
4. **Other-session attribution** consults live buffers only; an ended sibling's files become `git_observed`.
5. **Baseline timing for agents without a start hook** (Cursor background agents): baseline at first observed hook.
6. **`_parse_git_changed_files` cap**: hook fold now examines 200 rows and reports at most 50 + 50 excluded (`files_truncated`); import/wrap still cap at 20.
7. **Windows**: not executed here; CI covers it. New code avoids POSIX-only calls except the guarded `chmod`.
8. **Flake watch**: one unexplained single failure of the 24-thread concurrency test during an early, heavily loaded run; 8 subsequent runs were green. Lock timeouts under extreme load would surface as `action == "error"` rather than an identity collision.

## Recommendations intentionally deferred (see `docs/architecture/POST_V044_CORE_CLEANUP.md`)

Splitting `cli/main.py`, `adapters/claude_hooks.py`, `history/shard_contract.py` and `run/pipeline.py`; unifying `_now`, git and sanitisation helpers; consolidating the six record-quality concepts; narrowing broad excepts; removing PR-number comments; renaming `history/completeness.py`; possibly storing the pipeline's review-risk floor as a labelled fact. None were needed for the four integrity fixes and none were attempted.

## Release steps not performed (by instruction)

Merge to `main`, dated CHANGELOG heading (currently `0.4.4 - Unreleased`), tag `v0.4.4`, PyPI publish, GitHub Release, Windows smoke.
