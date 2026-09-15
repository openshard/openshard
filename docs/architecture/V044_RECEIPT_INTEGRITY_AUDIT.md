# v0.4.4 Receipt Integrity Audit

Baseline: `main` at `25de0b6102af6c62817da79f6658dad8b58acd66` (the v0.4.3
release commit; verified unchanged at audit time). Suite state before any
edit: 8834 passed, 3 skipped; ruff and mypy clean.

This audit records what the code does today, in the trust-critical
external-agent capture path only, and the smallest change proposed for
each of the four foundational problems. It is deliberately short.

## 1. Current identity model

| Identifier | Where minted | Derivation | Global-safe? |
|---|---|---|---|
| `shard_id` | `shard_contract._make_shard_id(timestamp, run_index)`; callers: `claude_hooks._ensure_record`, `wrap_exec`, `claude_code_import`, `run/_pipeline_helpers._log_run`, `run_attempt.resolve_shard_for_attempt`, `feedback.py`, `cli/main.py` | `shard-YYYYMMDD-NNNN` where `NNNN = runs.jsonl line count + 1` | **No.** Two repositories, two machines, or two concurrent sessions in one repository (both read the same line count before either appends) mint the same id. |
| `run_id` | `_ensure_record` (hooks), pipeline | `<started_at>-<session_id[:8]>` for hooks; timestamp otherwise | No (timestamp-based). |
| `capture.session_id` | agent | agent-minted session id, regex-validated | Per agent only; two agents can reuse an id (queues are keyed `<agent>.<sid>` for that reason). |
| `content_hash` | `shard_schema.coerce_shard_entry` (compute-if-missing) | SHA-256 of canonical JSON, unkeyed | Tamper-evidence only; proves nothing about authorship. |

`history/shard.py` already documents that persistent task identity across
attempts is future work; `run_attempt.resolve_shard_for_attempt` only
links attempts when an explicit, persisted `shard_id` is supplied.

## 2. Current change/file attribution model

`claude_hooks.build_hook_entry` -> `_git_changed_files`:

* `git diff <session-start HEAD> --name-status` plus `git ls-files --others
  --exclude-standard`, filtered for `.openshard/`, `.claude/`, `.codex/`,
  `.opencode/`.
* Every path in that diff becomes `files_detail[]` and a `file.changed`
  Event with `evidence = git_observed`, and is counted in
  `files_created/updated/deleted` -> the receipt's `Changed N files`.
* No working-tree baseline is taken at session start beyond
  `git_dirty: bool`. A file dirty before the session, a human edit during
  the session, or another agent's edit all appear as this session's
  changes.
* Agent-reported paths (`hook_files`: Claude `PostToolUse` success, Cursor
  `afterFileEdit`, OpenCode `file.edited`) are used **only** when git is
  unavailable; when git is available they are discarded.
* Codex has no positive file-success signal (`apply_patch` headers are the
  *attempted* target only).

## 3. Current capture-service trust boundary

`adapters/claude_capture_service.py`, loopback `127.0.0.1` only, stated
assumption: "anything that can connect to localhost is inside the trusted
user boundary". Consequences:

* `POST /hooks/{claude,codex,opencode,cursor}` and `POST /status/claude`
  accept any well-formed JSON from any local process or any browser page
  (a cross-origin `fetch` to `127.0.0.1:47811` is a simple request; the
  response is unreadable but the evidence is still recorded).
* `GET /health` (unauthenticated) returns `instance_id`; `POST /shutdown`
  is authorised by that same `instance_id`. Anyone who can read `/health`
  can stop the service.
* `X-OpenShard-Project-Dir` (any value) steers which repository the event
  is recorded in, bounded only by `_is_forbidden_capture_root`.

Transport capabilities of each integration (verified against vendor docs
where noted):

| Integration | Transport to the service | Can present a header secret? | Can read a file at hook time? |
|---|---|---|---|
| Claude Code hot hooks | HTTP hooks in `.claude/settings.local.json` | Yes: static `headers`; `$VAR` interpolation only for names in `allowedEnvVars`, resolved from Claude Code's own process env (docs). `CLAUDE_ENV_FILE` applies to Bash commands only. | No (no process of ours runs). |
| Claude Code `SessionStart`, status line | command hooks (`openshard hooks claude`, `claude-status`) | Yes, via our process | Yes |
| Codex | command hooks (`openshard hooks codex`) | Yes, via our process | Yes |
| Cursor | command hooks (`openshard hooks cursor`) | Yes, via our process | Yes |
| OpenCode | TS plugin `fetch()` in `.opencode/plugins/openshard.ts` | Yes, custom headers | Yes (Bun/Node `fs`) |

`.claude/settings.local.json` and `.opencode/plugins/openshard.ts` are
already added to `.git/info/exclude` by the installers
(`ensure_local_settings_ignored`); Claude Code documents it excludes the
former only when *it* creates the file.

## 4. Current durable queue / replay behaviour

* Blocking path: reduce -> append one JSON line to
  `.openshard/claude_sessions/<key>.queue.jsonl` (fsync) -> `200 {}`.
* Worker: `_drain_session` rotates the live file, `_replay_file` applies
  every line. A transient error (`OSError` on read, `apply_reduced_hook`
  returning `error`) leaves the file and schedules a retry (fixed 2 s,
  unbounded). Correct.
* **Defect (D):** `json.loads` failure, non-dict lines, lines without a
  `data` dict, and `ReducedHookPayload.from_dict(...) is None` are all
  `continue`d, `ok` stays `True`, the file is unlinked. `stats` gains no
  counter; the record gains no mark. `tests/test_claude_capture_service.py::
  test_corrupt_queue_lines_are_skipped` currently *asserts* this silence
  (`replay_errors == 0`).
* Dedup (`applied_ids`) and out-of-order replay are sound.

## 5. Current capture-completeness signals

| Signal | Where | Meaning today |
|---|---|---|
| `Shard.capture_depth` (`full` / `partial` / `unknown`) | `history/shard.py` | Depth of *what OpenShard could see*: hooks capture is always `partial`. Not a loss signal. |
| `capture.hook_events_dropped` | record | Events dropped past `_MAX_BUFFERED_EVENTS` (200). Stored, never rendered. |
| `capture.session_end_observed` | record | Whether `SessionEnd` fired. Rendered indirectly via `task_status`. |
| `stats.replay_errors` / `last_error` | service health | Transient replay failures. Not tied to a record. |
| `history/completeness.py` | stats | Field-presence heuristic over receipts (how many fields are filled), unrelated to evidence loss. |

There is no per-record statement "evidence for this session is known to be
missing or corrupt", and nothing in the receipt can say so.

## 6. Current Receipt claims (compact receipt, `shard_contract`)

* `RECEIPT — shard-YYYYMMDD-NNNN` header; `Shard ID` again in the footer.
* `Capture  partial — OpenShard did not execute or verify this run`.
* `Status  Completed` when one `Stop` fired (`task_completion`); the
  docstring says completion != verification, the word does not.
* `Changed N files` from the unattributed git diff (Section 2). Result
  line: `"<Agent> session: N file(s) changed, ..."`.
* `Risk`: `build_shard_receipt` coerces `Not recorded`/`Low` to `High` for
  `is_review_task` entries at display time, unlabelled (mirrors a run-time
  floor). Tests pin this (`test_native_receipt.TestReviewTaskRiskFloorInReceipt`,
  `test_last_rendering.test_review_task_flag_overrides_low_risk_in_last`).
* No integrity row; `content_hash` appears only in `--json`
  (`content_hash_status`) and `openshard shard verify`. Wording there is
  already "content hash", not signature.
* Trust Score is not on the receipt; it is behind `openshard trust last`
  and `--json`. Nothing to demote.
* Owner/requester/approver: no field exists and nothing infers one
  (grep for `getpass`/`user.name`/`USERNAME` in history/cli/adapters: no
  hits). `Approval` shows policy approval only.

## 7. Exact compatibility constraints

* `runs.jsonl` records are append/upsert only; old records lack every new
  field and must still render (`coerce_shard_entry` passes unknown keys
  through; renderers must treat new fields as optional).
* `shard_id` format and minting stay exactly as-is (history grouping,
  `get_shard`, MCP `get_shard`/`get_receipt`, `run_attempt` linkage,
  search all key on it).
* JSON envelopes (`last --json`, `history --json`, MCP `receipt_to_dict`)
  are additive only. Existing keys keep their meaning; `files_changed`
  may become *smaller* for new records (pre-existing files excluded) but
  its type and presence are unchanged.
* Queue-line format (`{"id","kind","at","data"}`) is unchanged so a queue
  left by 0.4.3 replays on 0.4.4.
* Buffer schema: additive keys only; pre-0.4.4 buffers keep working.
* Telemetry schema is a fixed-enum grammar; no new free-text property.
  Counters may be added to `capture.service` (`rejected`, `corrupt_lines`)
  because they are bounded ints and cannot carry the token.
* Hook installers must stay idempotent; an upgraded hook entry is reported
  as `updated`, unrelated hooks untouched.
* Coding agents stay fail-open: every hook path continues to exit 0 /
  return the required stdout when the service refuses or is absent.

## 8. Proposed implementation

### A. Pre-existing / concurrent changes (Phase 5)

* At buffer creation (first observed hook), snapshot a **working-tree
  baseline**: `git status --porcelain=v1 -z --untracked-files=all` ->
  path list, plus `git hash-object --stdin-paths` blob ids for the dirty
  paths (bounded; over the bound the baseline is marked `truncated`).
  Stored on the buffer as `baseline` (paths + blob ids + source + time).
* At fold, classify each git-diff path:
  * `agent_reported` — in `hook_files` (positive agent success signal);
  * `pre_existing` — in the baseline with an unchanged blob id ->
    **excluded** from counts;
  * `other_session` — reported by another live session buffer in the
    same repository -> excluded, listed;
  * `git_observed` — everything else (includes baseline paths whose blob
    changed, flagged `pre_existing_changes: true`; includes Codex
    "attempted" targets flagged `agent_attempted: true`). Actor unknown.
* Record: `files_detail[].attribution`, top-level `changes` block with
  counts and baseline metadata; `files_created/updated/deleted` count only
  `agent_reported` + `git_observed`. `file.changed` Events keep
  `evidence=git_observed` and gain `metadata.attribution`.
* Receipt: `Changed  2 files (1 agent-reported, 1 git-observed)` and a
  `Pre-existing  1 excluded` line when relevant; per-file letters keep
  working; `--json` carries the full provenance.

### B. Unauthenticated capture input (Phase 3)

One server-side contract: every `POST` needs
`X-OpenShard-Capture-Token` matching, in constant time, either the
per-user **capture token** or the **repository-scoped capability** derived
from it (`HMAC-SHA256(token, normalised repo root)`), or is answered
`401` with no evidence recorded. `Origin`/`Referer`-bearing requests are
answered `403` (browser defence in depth). `/health` stays unauthenticated
but is the only such endpoint and carries nothing that authorises anything
(`instance_id` remains informational; `/shutdown` requires the token).

* Token: 32 random bytes, hex, in `<OPENSHARD_HOME>/capture-token`
  (0600), created on first use by the service or a client; never in
  telemetry, logs, `/health`, or normal CLI output.
* Transports: our command-hook processes and `openshard capture stop`
  read the file. The OpenCode plugin reads the file at run time
  (`OPENSHARD_HOME` honoured). Claude Code HTTP hooks cannot read files, so
  the installer writes the **repo-scoped** capability into the header in
  `.claude/settings.local.json` (already git-excluded by the installer;
  installer additionally refuses when git reports the file tracked). A
  leaked settings file therefore authorises events for that repository
  only, and rotation (`openshard capture rotate-token`) invalidates it.
* Migration: `setup`/`capture install` rewrite hook entries (`updated`).
  The `SessionStart` command hook self-heals a repository whose Claude
  hook entries still lack the header (takes effect from the next Claude
  session; hooks are snapshotted per session). `doctor` reports
  `rejected_unauthenticated` counts from `/health`.

### C. Receipt identity collision (Phase 2)

* New `receipt_id = "rcpt_" + uuid4().hex` minted at record creation in
  every writer (`_ensure_record`, `wrap_exec`, `claude_code_import`,
  `_log_run`), never at display time. `shard_id` unchanged.
* `ShardReceipt.receipt_id: str | None`; compact/full renderers show it
  when present; `receipt_to_dict`, `last --json`, `history --json` add it;
  `get_receipt` / `get_shard` / `search_history` accept a `receipt_id`
  where a `shard_id` is accepted today (additive lookup).
* No `task_id`/`work_id`: no invariant supports it yet. Documented.

### D. Corrupt / lost evidence (Phase 4)

* `_replay_file` returns a structured result: applied / duplicates /
  `corrupt` lines. Each undecodable or unusable line is written (bounded,
  as-is: it is already reduced material or damaged bytes) to
  `.openshard/claude_sessions/quarantine/<queue-file>.<n>.jsonl` with a
  small metadata header; `stats.corrupt_lines` increments; valid
  neighbours are still applied; the queue file is then removed (its
  content is preserved in quarantine).
* The affected session (from the neighbouring valid line, else the queue
  stem) receives `apply_capture_loss(...)` -> `buf["capture_losses"]` and
  a fold, so the record carries
  `capture.completeness = {"status": "incomplete", "reasons": [...]}`.
* Completeness statuses reuse `full`/`partial`/`unknown` from
  `history/shard.py` and add `incomplete`. Reasons: `dropped_hook_events`,
  `corrupt_queued_event`, `session_end_not_observed` (stale sweep),
  `integration_limitation` (documented per agent). Derived at read time
  for old records from `hook_events_dropped` only, labelled as derived.
* Receipt: `Capture  Incomplete — 1 queued event could not be decoded`.
* Transient `OSError`/replay `error` keep the existing retry path and are
  never treated as corruption.

### E. Receipt wording (Phase 6)

* `Status`: `turn_completed` -> `Turn completed (unverified)`;
  `ended_no_turn` -> `Session ended (no turn observed)`.
* Remove the display-time review-task risk floor; show the stored value.
* Add `Integrity  Matches (content hash)` / `Mismatch` / `Not recorded`
  from `verify_shard_hash`. Never the word signature.
* Add `Receipt ID` row; keep `Shard ID`.
* Test isolation: `conftest` repoints `DEFAULT_PORT` at a free ephemeral
  port for every test so no test can reach a developer's real service.

## 9. Outcome and validation status (post-implementation)

Recorded when the v0.4.4 branch was prepared for review; the release
checklist carries the steps that remain.

### What shipped against each issue

| Issue | Result |
|---|---|
| A. Pre-existing / concurrent changes | Baseline snapshot at the first observed hook (taken when `SessionStart` is received by the capture service); every git-diff path classified `agent_reported` / `git_observed` / `pre_existing` (excluded) / `other_session` (excluded); counts cover only this session's changes. |
| B. Unauthenticated capture input | Per-user token (0600) plus capabilities scoped to repository **and** agent (`r2.` HMAC), checked in constant time against the agent the receiver records under; browser headers refused; shutdown token-only; refusals counted and logged (throttled). Claude Code HTTP hooks and the OpenCode plugin carry scoped capabilities; Codex/Cursor run our own process. Agents stay fail-open. |
| C. Receipt identity | `receipt_id` (`rcpt_` + UUID4 hex) minted at creation by every writer; `shard_id` unchanged; no task identity added. |
| D. Corrupt / lost evidence | Undecodable queue lines quarantined (bounded) and counted; affected record `capture.completeness.status = incomplete`; valid neighbours applied; transient errors keep the retry path. Depth (`full`/`partial`/`unknown`) and completeness (`complete`/`incomplete`/`unknown`) are separate facts; pre-0.4.4 records read `unknown`. |
| E. Receipt wording | `Turn completed (unverified)`; recorded risk only; `Integrity  Matches (content hash)`; both identities shown; no owner/requester fabricated. |

### Validation performed

| Check | Status | Evidence |
|---|---|---|
| Linux, Python 3.11 (ruff, mypy, full pytest) | verified | full suite green on the reviewed tree; ruff and mypy clean |
| Claude Code live smoke (2.1.270) | verified | two `claude -p` sessions through `openshard setup` in temp repositories: all hook events accepted, receipt/JSON as designed; credential-stripping migration path exercised (refused -> `doctor` diagnosis -> `SessionStart` self-heal) |
| Codex, Cursor, OpenCode live smoke | not exercised | CLIs unavailable in the validation environment; fixture-driven tests only. The rendered OpenCode plugin was syntax-checked under node 22; the node-executed plugin tests need node >= 23. |
| Windows, Python 3.12 | not exercised locally | CI matrix covers both. Windows-sensitive new code: token file mode (`chmod` skipped on win32), `normalise_root` (`normcase`). |
| Test isolation from a developer's real capture service | verified | `conftest` repoints `DEFAULT_PORT` per test; pinned by `tests/test_v044_test_isolation.py` |

### Observations worth keeping

* Claude Code's CLI fires a `SessionEnd` HTTP hook for `claude mcp get` /
  `claude mcp list`. `doctor` and `setup` run `claude mcp get openshard`,
  so on a repository whose hooks lack a valid capability each `doctor` run
  adds one refused request. Correct and harmless (a `SessionEnd` with no
  work is never recorded); disappears once the hooks are upgraded.
* One intermittent failure of the focused v0.4.4 tests was seen only while
  another full suite ran concurrently on the same machine and never in
  isolation; the 24-thread identity test now raises its runs.jsonl lock
  budget so lock latency cannot masquerade as an identity failure.

### Known limitations carried forward

* Capabilities live in repo-local, git-excluded files (Claude hooks,
  OpenCode plugin): a copied file leaks a per-repository, per-agent
  capability; rotation invalidates it.
* Sessions in flight during an upgrade lose HTTP-hook evidence until the
  next session; the in-process fallback covers command hooks only.
* Other-session attribution consults live buffers only; an ended
  sibling's files become `git_observed`.
* Agents without a start hook (Cursor background agents) get their
  baseline at the first observed hook.
* The hook fold examines up to 200 diff rows and reports 50 + 50 excluded
  (`changes.files_truncated`); import/wrap adapters still cap at 20.
* Full cleanup recommendations: `POST_V044_CORE_CLEANUP.md`.
