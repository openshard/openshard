# OpenShard v0.5.0 Foundation Audit

Baseline audited: v0.4.3 (`25de0b6`). Written before any v0.5 code was
changed. It answers four questions: what already exists, what to reuse, what
is missing, and what must not be rebuilt. The last section is the recommended
v0.5 architecture and the backwards-compatibility rules every v0.5 change
follows.

## 1. Shape of the codebase

OpenShard is a single Python package (`openshard/`, ~57k lines, Python 3.11+,
`click` CLI, `rich`/`textual` UI, `httpx`, `pyyaml`). There is no database, no
server, no JS. Persistence is append-only JSONL under `.openshard/` in the
repository being worked on:

| File | Written by | Read by |
| --- | --- | --- |
| `runs.jsonl` | `run/_pipeline_helpers._log_run` (OpenShard-routed runs) and every capture adapter (`adapters/claude_hooks.build_hook_entry`, Codex, Cursor, OpenCode, `wrap_exec`, `claude_code_import`) | everything: `openshard last/history/stats/proof/trust/ci`, MCP server, TUI |
| `feedback.jsonl`, `session_signals.jsonl`, `sandbox_apply_receipts.jsonl`, `native_steps`, checkpoints | feedback CLI, session inference, sandbox apply | receipts (read-time joins), trust score |
| `~/.openshard` (per-user) | capture service state, telemetry queue/backoff | capture service, telemetry |

All writes funnel through `history/jsonl_store.py` (cross-platform locked
appends and upserts). One run record = one JSON object = one Shard attempt.

## 2. What already exists (and is good)

### 2.1 Receipt model: a two-layer design already exists

* **Persisted record** (the "entry"): a flat dict with `schema_version`
  (`SHARD_SCHEMA_VERSION = "1.2"`), stamped through
  `history/shard_schema.coerce_shard_entry` on write and on load. Blocked
  fields (raw prompts, diffs, transcripts, stack traces) are stripped
  recursively. A `content_hash` (`sha256:` over canonical JSON,
  `history/shard_hash.py`) is stamped at write time and verified on read
  (`valid | mismatch | missing`).
* **Derived receipt**: `history/shard_contract.ShardReceipt` is a read-time
  projection built by `build_shard_receipt(entry)` (never raises), embedding
  the canonical `Shard` identity (`history/shard.py`: `shard_id`,
  `agent`, `origin`, `capture_depth`) and rendering through
  `render_compact_shard_receipt` / `render_full_shard_receipt`.
* **Machine views**: `history/views.receipt_to_dict` is the single privacy
  boundary used by MCP and `--json`; `cli/main._export_run_entry` is the
  richer `last --json` export; `_machine_envelope` gives every `--json`
  command the same envelope (`schema_version: "1"`).

Already modelled on the entry, and reused untouched by v0.5:

| Concept | Existing field(s) | Notes |
| --- | --- | --- |
| Task / intent | `task`, `summary`, `plan` | free text, truncated for display |
| Executing agent | `executor`, `workflow`, `adapter`, `capture.agent`, `capture.agent_vendor` | `derive_shard_identity` maps to a label + origin + capture depth |
| Model / provider | `execution_model`, `routing_selected_model`, `routing_selected_provider`, `stage_runs[].model`, `capture.models_seen`, `capture.provider` | multi-model sessions are already represented |
| Repository / branch / commit | `repo_name`, `repo_identity` (host/owner/repo), `git_branch`, `git_head_commit_hash`, `git_base_branch`, `git_base_commit_hash`, `git_dirty` | additive fields from 0.4 |
| Timestamps / duration | `timestamp`, `run_id`, `duration_seconds`, `capture.started_at`, `last_activity_at` | `run_id == timestamp` for pipeline runs |
| Attempts / retries | `shard_id`, `attempt_number`, `retry_triggered`, `retry_*_tokens`, `retry_estimated_cost`, `history/run_attempt.py` | a retry is a sibling entry with the same `shard_id` |
| Permissions actually enforced | `command_policy.{allowed_paths,blocked_paths,blocked_commands}`, `write_path`, `form_factor.read_only` | enforcement-side view only |
| Policy decisions | `policy_decisions[]` = `policy/decision.PolicyDecision` (`decision_id, action, resource, decision ∈ {allow, ask, deny, not_applicable}, reason, source, severity, approval_required, approval_granted, scope, created_at`), resolved by `resolve_policy_decisions` (deny > ask > allow) | **ALLOW/DENY/ASK with a reason already exists.** |
| Approval | `approval_request` (`requires_approval`, `action`, `prompt`), `approval_receipt` (`granted`, `reason`) | no approver identity, timestamp or mechanism |
| Verification | `verification_attempted`, `verification_passed`, `review_checks[]` (`name, status, summary, reason`), `osn_verification_contract` (`status ∈ {not_run, passed, failed, skipped, impossible, manual_review, unknown}`, per-check lists, returncode, duration), `verification_plan` | `proof_signals.verification_status_from_receipt` is the canonical reducer |
| Evidence | `files_detail`, `file_context.paths`, `evidence_capsules[]`, `secret_scan_result`, `events[]` (`history/event.py`, with `evidence ∈ {directly_observed, agent_reported, git_observed, independently_verified}`), `provenance` (read-time) | evidence *kind* is already first-class |
| Capture completeness | `Shard.capture_depth ∈ {full, partial, unknown}`, `capture.hook_events_dropped`, `capture.task_status`, `history/completeness.py` field-presence score | per-receipt, no fleet-level coverage claim |
| Cost | `estimated_cost`, `prompt_tokens`, `completion_tokens`, `cache_*_tokens`, `stage_runs[].cost`, `cost_provenance`, `tokens_provenance`, `capture.cost_total_usd` | generation cost only; no verification/retry split at receipt level |
| Outcome (post-run) | `developer_feedback.outcome` (feedback.jsonl: `accepted, rejected, partial, useful, wrong, needs-retry`), `session_signals` (`interaction.accepted/rejected/edited`), `sandbox_apply_receipts` | no merged/deployed/rolled-back |
| Integrity | `content_hash` | tamper evidence only; explicitly *not* a signature |

### 2.2 Proof, trust and CI layers over the receipt

* `history/proof_contract.py` (`SHARD_PROOF_CONTRACT_VERSION = "1.0"`):
  17 named proof sections, each `present | partial | missing | unknown |
  not_applicable | unsafe`, overall `strong | usable | partial | weak |
  unsafe | unknown`. Pure, JSON-only.
* `history/trust_score.py`: a 0..100 score built from explicit penalties
  (verification failed/not run, policy denied, manual review, secret scan,
  low completeness, rejected feedback...). It is a *consumer* of the receipt,
  never a proof section, and every penalty carries a code and reason.
* `ci/policy_check.py`: `pass | warn | fail | skip` for CI.
* `history/shard_quality.py`: one-sentence quality summary.

The project already follows the principle the v0.5 brief restates: the trust
score is explicitly a convenience derived from listed evidence, not the primary
mechanism.

### 2.3 Policy, gates and approvals

* `execution/gates.py`: `ApprovalGate` with `approval_mode ∈ {smart, auto,
  ask}` (from `config.yml`), risky paths, cost threshold, shell commands,
  stack mismatch. Decisions are `GateDecision`s resolved by
  `resolve_gate_decisions`.
* `policy/runtime.build_runtime_policy_decisions` turns approval request /
  receipt, read-only, secret scan and validator signals into persisted
  `policy_decisions`. Sources today: `approval_gate`, `path_policy`,
  `secret_scan`, `validator`.
* `native/retry_diagnosis.py`: `status ∈ {not_needed, allowed, used,
  exhausted, blocked, manual_review, unknown}` with allowed/blocked changes.

### 2.4 Routing and escalation

`routing/model_resolver.py` defines symbolic roles (`cheap`, `main`,
`strong`, `escalate`, ...) and `ESCALATION_CHAIN = [MODEL_STRONG,
MODEL_ESCALATE]`. `routing/profiles.py` escalates `native_light ->
native_deep` from history pass/retry rates. Escalation exists as a routing
concept; it is **not** recorded on the receipt as "attempt 1 failed on X,
attempt 2 succeeded on Y".

### 2.5 Network and privacy conventions

* Telemetry (`telemetry/`): stdlib `urllib` HTTPS POST, strict timeouts,
  exponential backoff file, HTTPS-only (loopback HTTP for tests), schema with
  no free-text fields, `RecordingTransport` for tests, off in CI /
  `DO_NOT_TRACK`. This is the template for any sync transport.
* `safety/sanitize.py`: path and secret redaction for anything rendered.
* MCP server (`mcp/server.py`): read-only, local, uses `history/views`.

### 2.6 Tests, CI, packaging

* ~8,700 tests under `tests/` (unittest + pytest, `CliRunner`,
  isolated filesystems). CI: ruff, mypy (non-strict), pytest on Ubuntu and
  Windows, Python 3.11 and 3.12.
* Packaging: `pyproject.toml` (setuptools), version `0.4.3`, extras
  `anthropic`, `openai`, `mcp`, `dev`. Release via `release.yml`.
* Docs: `docs/what-is-a-shard.md`, `agent-capture.md`, `telemetry.md`,
  `cli-reference.md`, demo scripts, release checklist.

## 3. Existing cloud / sync / team concepts

None in code. README lists "No hosted team platform yet / No cloud sync yet /
No hosted dashboard for teams yet" and the roadmap names hosted run history,
team policies, shared approval gates and dashboards. The only outbound
network path is telemetry. There is no user, organisation, team, owner or
approver concept anywhere in the package.

## 4. What is missing for the 19 questions

| # | Question | Status in 0.4.3 |
| --- | --- | --- |
| 1 | What was the agent asked to do? | Present (`task`) |
| 2 | Who owns this work? | **Missing** |
| 3 | Who requested it? | **Missing** |
| 4 | Which agent/model executed it? | Present |
| 5 | What was it allowed to do? | Partial (enforced paths/commands only; no requested/used/denied permission sets) |
| 6 | Which policy governed the run? | Partial (decisions carry a `source`, but no policy identity or version) |
| 7 | Was approval required? | Present |
| 8 | Who or what approved it? | **Missing** (no approver, timestamp, mechanism) |
| 9 | What actually happened? | Present (files, events, timeline) |
| 10 | What evidence was captured? | Present (evidence kinds, capsules, provenance) |
| 11 | What independent checks verified the result? | Partial (`independently_verified` evidence kind exists; no verifier identity/type per check) |
| 12 | Was capture complete? | Partial (`capture_depth`, dropped hook events; no coverage claim) |
| 13 | Did the run succeed? | Present (verification + status) but no single receipt **state** |
| 14 | What did generation cost? | Present |
| 15 | What did verification cost? | **Missing** |
| 16 | Were retries required? | Present (siblings), not summarised on one receipt |
| 17 | Total cost of the verified successful task? | **Missing** (cost-per-pass exists only in evals) |
| 18 | What happened afterwards? | Partial (feedback outcomes); no merged/deployed/rolled back |
| 19 | Can the receipt be proven unaltered? | Partial (content hash; no signature or chain) |

Also missing: a receipt **state** enum (`VERIFIED`, `UNVERIFIED`,
`VERIFICATION_FAILED`, `BLOCKED`, `APPROVAL_REQUIRED`, `APPROVED`,
`VERIFIED_AFTER_RETRY`, `VERIFIED_AFTER_ESCALATION`) derived from evidence;
and model escalation recorded across attempts.

## 5. What must NOT be rebuilt

* The persisted entry format and `coerce_shard_entry`. Every reader, adapter
  and 8,700 tests depend on it. v0.5 adds optional namespaced keys only.
* `ShardReceipt` and its two renderers. They are the human contract and are
  pinned by many rendering tests. v0.5 extends them additively.
* `PolicyDecision` (`allow/ask/deny/not_applicable` + reason + source). It
  is the right shape; v0.5 adds policy identity around it, not a new enum.
* `Event` and evidence kinds. Verifier independence should reuse
  `independently_verified` rather than a parallel vocabulary.
* `content_hash`. Integrity in v0.5 keeps it and adds an optional
  signature/attestation slot beside it, never replacing it.
* Proof contract, trust score, completeness, CI check, quality summary.
  They consume the receipt; they gain a state, they are not replaced.
* JSONL store, capture service, adapters, telemetry transport pattern.

## 6. Backwards-compatibility risks

1. **Schema version pins.** ~20 tests assert `schema_version == "1.2"` for
   entries written by adapters. Bumping `SHARD_SCHEMA_VERSION` is a v0.5
   decision, but it is not required for additive fields: `coerce_shard_entry`
   passes unknown keys through. Decision: keep `"1.2"` on the write path in
   this branch; the v0.5 receipt contract carries its own version
   (`RECEIPT_CONTRACT_VERSION = "2.0"`). A bump to `"1.3"` can land with the
   release once producers actually write the new fields.
2. **Rendering tests** pin exact lines of the compact receipt. New rows must
   be conditional on new data being present, or added to the full view only.
3. **`content_hash`.** Any field added at write time changes the hash of new
   records only. Old records keep their stored hash and still verify.
   Read-time derivations (state, cost breakdown) must never be persisted
   back into the entry.
4. **`receipt_to_dict` default key set** is the MCP contract and must stay
   stable; new keys go under `extended=True` or a new nested key.
5. **`last --json`** is consumed by agents; additions must be additive keys
   inside the envelope.
6. **Blocked fields.** New nested blocks must not introduce keys named like
   `SHARD_BLOCKED_FIELDS` and must never carry raw prompts, diffs or output.

## 7. Recommended v0.5 architecture

### 7.1 Receipt contract v2 (public repo, additive)

`openshard/history/receipt_contract.py`: a read-time `ReceiptContract`
built from an entry (plus optional sibling attempts), versioned `"2.0"`,
never raising, JSON-serialisable via `to_dict()`. It groups the 19 answers
into named blocks: `identity`, `actors` (owner / requested_by / executed_by /
approved_by as `Principal`s), `permissions`, `policy`, `approval`,
`verification`, `capture`, `attempts` (with escalation), `cost`
(generation / verification / retry / total / per verified success),
`outcome`, `integrity`, and a derived `state` + `state_reason`.

New **optional** persisted keys (all top-level, namespaced, ignored by 0.4.3
readers): `actors`, `permissions`, `policy`, `approval` (approver, timestamp,
mechanism), `verifiers`, `escalation`, `cost_breakdown`, `outcome`,
`attestation`. Producers that do not know them write nothing; the contract
derives what it can from 0.4.3 fields.

Human output: the full receipt gains a `RECEIPT STATE` block and, when
present, `ACTORS`, `COST BREAKDOWN`, `CAPTURE`, `OUTCOME`. The compact
receipt gains one `State` row. `last --json` gains `receipt_contract`.

### 7.2 Contracts package (public repo)

`openshard/contracts/`: `Protocol`s and small request/result dataclasses for
verification, policy evaluation, approvals, sync, managed compute and
outcome reporting. No implementations beyond in-memory/no-op ones used by
tests. Local runtime keeps working with none of them configured.

### 7.3 Sync (public client, private server)

`openshard/sync/`: an explicit, opt-in client (`openshard sync push`) that
sends `ReceiptContract.to_dict()` plus the raw entry's safe projection to
an HTTPS endpoint with a bearer token from the environment. Same transport
rules as telemetry (HTTPS-only, timeouts, never on a hook path). Off unless
configured; never sends blocked fields.

### 7.4 OpenShard Cloud (private repo `openshard-cloud`)

Python stack, consistent with the ecosystem: FastAPI + SQLAlchemy 2 +
Alembic, SQLite for development and tests, PostgreSQL for production;
server-rendered dashboard (Jinja2) so the first slice needs no JS build.
Core tables: `users, organisations, memberships, teams, projects, agents,
receipts, receipt_attempts, evidence, verifications, policies,
policy_decisions, approvals, managed_compute_runs, usage_records, api_tokens`.
Receipts are stored with the raw synced JSON plus indexed columns derived by
the same `receipt_contract` code (vendored dependency on `openshard`), so
local and hosted agree on state.

### 7.5 Explicitly deferred

Signing / attestation chains, real policy evaluation engines, RBAC
enforcement beyond membership roles, Managed Compute providers, outcome
webhooks (merged/deployed) and outcome-aware routing are given interfaces
and TODOs only.

## Appendix: adapter capability matrix (from the code survey)

| | Claude hooks | Codex hooks | Cursor hooks | OpenCode plugin | wrap_exec | claude_code_import |
| --- | --- | --- | --- | --- | --- | --- |
| Builder | `build_hook_entry` | same | same | same | `build_wrap_entry` | `build_claude_code_import_entry` |
| `executor` | `claude_code_hooks` | `codex_hooks` | `cursor_hooks` | `opencode_plugin` | `claude_code_wrap` | `claude_code_import` |
| Model | status line | hook `model` | `model_id` | `model_id` | `--model` only | `--model` only |
| Provider | no | no | no | `provider_id` | no | no |
| Cost | cumulative minus baseline | no | no | per-message sum | no | no |
| Tokens | 4 counters | no | no | 4 counters | no | no |
| Tool success attested | `PostToolUse` | no | no | `file.edited` | n/a | n/a |
| Run success | none (`run.completed`/unknown) | none | none | none | exit code | none |
| `verification_passed` | always `None` | `None` | `None` | `None` | absent | `None` |
| Write mode | upsert by `(executor, session_id)` | same | same | same | append | append |

Consequence for v0.5: every externally observed adapter yields
`capture.coverage = partial` and `verification.independent = False` unless a
producer adds `verifiers`; only OpenShard-executed runs can reach `complete`.
