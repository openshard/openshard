# OpenShard v0.4.4 review checklist

Legend: **PASS** verified on this branch; **FAIL** verified broken; **NOT TESTED** no verification possible in this environment (says why).

| Item | Result | Evidence |
|---|---|---|
| Receipt ID collision resistance | **PASS** | `tests/test_v044_receipt_identity.py`: 10k ids distinct; 24 concurrent sessions in one repo → 24 distinct `receipt_id` while historic `shard_id` collides across repos as expected; stable across folds and rebuilds. |
| Dirty-tree attribution | **PASS** | `tests/test_v044_change_attribution.py`: tracked dirty file and untracked file before session → `pre_existing`, excluded from counts; pre-existing file changed again → `git_observed` + `pre_existing: true`; deleted pre-existing excluded. Live Claude Code smoke: `Excluded  2 pre-existing`. |
| Concurrent-session attribution | **PASS** | Two live sessions in one repo: each other's agent-reported files → `other_session`, excluded; a later session treats an earlier session's uncommitted work as `pre_existing`. Git-only changes are `git_observed` with "actor not established", never agent-attributed. |
| Capture authentication | **PASS** | `tests/test_v044_capture_auth.py` (17) + updated service tests: 401 without/with malformed credential and nothing recorded; 403 on browser headers; repo capability scoped to its repo; shutdown needs the token; health carries no credential; token absent from telemetry grammar, logs, state, CLI output. Live: forged POST → 401, `doctor` diagnosis, SessionStart self-heal. |
| Corrupt evidence handling | **PASS** | `tests/test_v044_evidence_loss.py` (12): corrupt lines quarantined (bounded, no absolute paths), counted, record `incomplete` with reason, neighbours applied, queue removed only when settled; transient `PermissionError` / replay `error` retried, never quarantined. |
| Capture-completeness visibility | **PASS** | Receipt renders `Capture  Incomplete — 1 queued event could not be decoded` above the partial line; JSON/MCP carry `capture_completeness`; old records derive and label `derived`. |
| Old-history compatibility | **PASS** | Records without `receipt_id`/`changes`/`completeness` render unchanged (`test_v044_receipt_identity.py`, `test_v044_change_attribution.py::TestCompatibility`, `test_routing_truth.py::test_old_record_renders_without_error`, existing golden tests); `receipt_id` is never back-filled; 0.4.3 queue lines replay. |
| Telemetry privacy unchanged | **PASS** | Schema still enum/int-only; two bounded counters added; `schema.token()` rejects the capture token; `tests/test_telemetry*.py` green; no new event types. |
| Claude live smoke | **PASS** | Claude Code 2.1.270, `claude -p` in a temp repo through `openshard setup`: 5 hook events accepted (0 refused), receipt and JSON as designed; migration path (stripped credential → refused → self-heal) exercised live. |
| Codex live smoke | **NOT TESTED** | Codex CLI not installed in this environment. Fixture-driven tests only. |
| Cursor live smoke | **NOT TESTED** | Cursor CLI not installed in this environment. Fixture-driven tests only. |
| OpenCode live smoke | **NOT TESTED** | OpenCode not installed in this environment. Plugin header change verified by reading the rendered plugin source; node-executed plugin tests run only where node is available. |
| Windows | **NOT TESTED** | No Windows runner here; CI matrix covers `windows-latest` × 3.11/3.12. Windows-sensitive new code: token file mode (chmod skipped on win32), `normalise_root` (`normcase`), no new POSIX-only calls. |
| Linux | **PASS** | Full suite, ruff and mypy green on Linux / Python 3.11 (see `V044_IMPLEMENTATION_REPORT.md`). |
| Release readiness | **NOT TESTED** (by instruction: stop before release) | Branch pushed, no merge/tag/publish. Remaining before release: CI on Windows and 3.12, dated CHANGELOG heading, Codex/Cursor/OpenCode live smokes per `docs/release-checklist.md`. |
