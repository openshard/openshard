# Historical Ingestion v1 — Architecture

Status: **v1 implemented in Core** (`openshard/ingest/`, `openshard ingest`): foundation, Claude Code history, Codex history, and local git evidence. §16 records how the §15 open decisions were settled for v1 and where the implementation differs from this design. The sync contract and the Platform are unchanged. Items marked *(proposed)* further down are kept for context; §16 overrides them where they differ.

## 1. Why

Today OpenShard creates receipts only going forward:
- live hooks (the `adapters/claude_hooks.py` pipeline)
- `import claude` / `wrap claude`, which take a git snapshot of *now*
- native runs
- Grok Bot OTLP/self-report ingest

Nothing reads past sessions. The hook translators deliberately refuse `transcript_path`, and `SHARD_BLOCKED_FIELDS` strips `transcript`, `raw_prompt` and similar fields (`history/shard_schema.py`).

A new user therefore starts with an empty history, even though months of Claude Code, Codex, and other sessions already exist on disk, in cloud drives, and in CI.

Historical Ingestion rebuilds useful, honestly labelled receipts from that evidence, wherever it lives:
- every fact states where it came from;
- OpenShard never claims it observed anything live;
- sealed receipts never change.

### Existing pieces this builds on

| Concern | Existing code |
|---|---|
| Batch-ingest precedent | `adapters/grok_bot.py`: `ingest_log_records` (group per conversation, dedupe on event id, upsert, `IngestResult`) |
| Record write path | `coerce_shard_entry` (`history/shard_schema.py`), `ensure_receipt_id` (`history/receipt_identity.py`), `append_jsonl` + `history_file_lock` (`history/jsonl_store.py`) |
| Integrity | `content_hash` / `verify_shard_hash` (`history/shard_hash.py`) |
| Events and evidence | `make_event` + `EVIDENCE_*` (`history/event.py`), `build_verification` / `not_observable_verification` (`history/verification.py`) |
| Scrubbing | `scrub_text_for_secrets` (`security/secret_scan.py`), `sanitize_text` / `sanitize_path` (`safety/sanitize.py`), `history/task_title.py`, the command reducer in `reduce_hook_payload` |
| Repo identity | `capture_repo_identity` (`history/repo_identity.py`), `run_git` (`util/git.py`) |
| Agent metadata | `AgentProfile` (`adapters/capture_agents.py`), `_EXTERNAL_AGENT_LABELS` (`history/shard.py`) |
| Sidecar precedent | Verification v2 `openshard verify` → `.openshard/verifications.jsonl` (writes evidence without rewriting hashed receipts) |
| Sync | `sync/envelope.py` (a closed key set shared with the platform `packages/contracts`), `sync/outbox.py` (a `content_hash` change makes a synced receipt `stale`) |

### Gaps this design must close

- The import path never dedupes; it always appends.
- The hook fold (`build_hook_entry`, `_new_buffer` → `collect_git_info`) diffs the **current** working tree. That is wrong for past sessions, so historical ingestion must not reuse `_fold`.
- `shard_id` is derived from line position.
- There is no job/checkpoint machinery.
- There is no producer yet for git-verified or CI-verified evidence (`SOURCE_GIT_VERIFIED`, `MODE_CI_REPORT` and `artifact_sha` are reserved but unused).

## 2. Invariants

These are permanent and enforced by tests.

1. **No raw transcript persistence.** Transcripts and other source bytes are streamed and processed in memory. Nothing raw is written to `.openshard/`, job files, logs, telemetry, sync payloads, or hosted storage. Only reduced, scrubbed facts, a SHA-256 of the source bytes, and a locator are kept. Transient processing is fine; persistence is not.
2. **Never `directly_observed`.** Historical ingestion never emits `directly_observed` (or `openshard_executed`) evidence. Imported evidence always ranks below what OpenShard observed live.
3. **Every fact has a provenance.** `Fact = {value, evidence, source_ref}`.
   - A fact that cannot be recovered is recorded as `unknown` / not observable.
   - It is never silently omitted and never guessed.
4. **Sealed receipts are immutable.** Historical receipts are append-only writes, sealed with `content_hash`, and never upserted.
5. **Enrichment is append-only.** Later evidence is stored as attachments that pin `(receipt_id, content_hash)`. Assessments are versioned projections computed at read time.
6. **Idempotent.** Re-running any job is a no-op unless the source bytes changed.
7. **A connector is not a parser.**
   - A connector knows *where* the bytes live.
   - A parser knows *what format* they are in.
   - Drive, S3, Dropbox and uploads are all "blob connectors" feeding the same parsers.
8. **One parser implementation.** Parsers are written in Python, in Core. A future hosted worker runs the same package; there is no second implementation in TypeScript.

### Defaults, not invariants

The following are *defaults* and may change later.

- **One native session = one Shard (v1 default).** A Shard represents one meaningful engineering task. It may eventually combine several historical sessions or runs when there is **strong evidence** they belong to the same task. Examples:
  - an explicit `--shard` / `--task-id`
  - Claude `--resume` / `parentUuid` continuation chains
  - the same branch plus a verified commit lineage plus overlapping files within a short window
  - a PR that references several sessions

  v1 groups conservatively (one session → one Shard, attempt 1). The `ShardBuilder` is a replaceable policy, and grouping is recorded as a fact with its own evidence (`grouping: {rule, evidence}`) so a later regrouping can be attached rather than rewritten.

  Claude sidechain/subagent records fold into their parent session.
- **Current-repo-only routing** (see §4).
- **30-minute quiescence window.** Sources modified more recently than this are skipped because they may still be live.

## 3. Evidence model *(proposed; finalize after Verification v2 review)*

Historical ingestion needs two things:
- a **channel** label that says the fact came from an import;
- an **evidence strength** taken from the existing ladder.

| Evidence | Meaning | Typical historical facts |
|---|---|---|
| `imported_transcript` *(new)* | The agent host's own log recorded this at the time, and OpenShard read it later. **Below `directly_observed`.** | a tool or command was invoked; timestamps; model id; token usage; the cwd and branch recorded by the agent |
| `agent_reported` | Claims or outcomes asserted by the agent or its host | assistant prose ("tests pass"); transcript-recorded **command outcomes** (see below) |
| `git_observed` | Found in git, but the link to this session is not certain | commits in the session window that touch the edited files |
| `git_verified` | The git object exists and is strongly linked to the session | a SHA that appears in the session's tool output, exists (`cat-file -e`), is reachable from the branch, and whose diff overlaps the session's edits |
| `independently_verified` | An external system recorded it | CI check runs or statuses for a verified commit (`MODE_CI_REPORT`) |
| `estimated` (cost only) | OpenShard derived it from a pricing table | cost computed from tokens, always shown as "est." |
| `unknown` | Not observable from the available evidence | approvals when the log does not record them |

**Alignment with Verification v2.** Verification v2 classifies every hook-reported command outcome as `agent_reported`, and only `openshard verify` yields `directly_observed`. For consistency, a pass/fail outcome read from a transcript (e.g. Claude `tool_result.is_error`, Codex `function_call_output`) is also `agent_reported`. Only the fact that the invocation *happened* is `imported_transcript`. The v2 rule that an ambiguous status (not completed, or a conflict) resolves to `unknown` applies unchanged.

**Origin.** Add *(proposed)* `ORIGIN_HISTORICAL_IMPORT = "historical_import"` to `history/shard.py`, with `capture_depth` always `partial`. Receipts render a **"Reconstructed from history"** badge.

## 4. Pipeline

```
SourceConnector.discover() ──► SourceObject stream
  └─► Parser.sniff()/parse() ──► ParsedSession (format-specific, in memory, transient)
       └─► Normalizer ──► HistoricalSession (neutral IR; every field a Fact; scrubbed)
            └─► Enrichers (local git; later GitHub/CI) ──► Facts (pre-seal) | Attachments (post-seal)
                 └─► Deduper (import_key + source_sha256; live-capture collision)
                      └─► ShardBuilder (grouping policy + repo routing)
                           └─► ReceiptBuilder (coerce, receipt_id, events, seal, append)
JobRunner drives each item with checkpoints, retries, and cancellation.
```

New package: `openshard/ingest/`.

| Module | Responsibility |
|---|---|
| `connectors/base.py` | `SourceConnector` protocol: `kind`, `describe()`, `check_access() -> AccessStatus`, `discover(cursor, filters) -> Iterator[SourceObject]`, `open(obj) -> BinaryIO` (streaming), `stat(obj)`. `SourceObject{connector, object_id (stable), locator, size, mtime, etag, hint}`. The cursor is opaque, so cloud connectors (Drive page tokens, S3 continuation tokens) fit unchanged. |
| `parsers/base.py` | `Parser` protocol: `name`, `version`, `sniff(head_bytes, obj) -> float`, `parse(stream, obj) -> Iterator[ParsedSession]`. Parsers are pure, with no IO beyond the stream. Unknown record types are counted as `losses` and never crash the parse. |
| `model.py` | `Fact`, `SourceRef{source_sha256, ref}`, `HistoricalSession{native_session_id, agent, window, cwd, repo_hint, branch, head, task, model, provider, tokens, tool_calls, commands, file_edits, test_runs, approvals, claims, losses}` |
| `normalize.py` | Converts a `ParsedSession` into a `HistoricalSession`. **This is the scrub boundary**: `scrub_text_for_secrets` on every string, and `sanitize_path` to make paths repo-relative. It reuses the hook command reducer (`command_kind` test/lint/other) and `task_title`. |
| `enrich/git_local.py` | Historical git evidence (§6) |
| `dedupe.py` | Checks `import_key` + `source_sha256`, and detects collisions with live capture |
| `shard_builder.py` | Grouping policy and repo routing |
| `receipt_builder.py` | `coerce_shard_entry` → `ensure_receipt_id` → embedded events → seal → `append_jsonl` under `history_file_lock`. **It never calls `upsert_jsonl`.** |
| `jobs.py` | State machine, checkpoints, retries, cancellation |
| `registry.py` | Connector and parser registry. New entries do not touch the pipeline. |

**Storage neutrality.** The pipeline talks to three small protocols:
- `HistoryStore`: append receipt, append attachment, look up an `import_key`
- `JobStore`: job and item log
- `SourceConnector`

The local implementations are JSONL and filesystem. A hosted implementation plugs in behind the same protocols. No connector or parser imports a storage module.

**Repo routing.**
- A session's `cwd` resolves to its git root, and that root's `.openshard/` is the destination.
- By default, only sessions for the current repo are imported. `--all-repos` writes only into repos that already have `.openshard/`.
- A git root equal to `$HOME` is refused unless `--allow-home-repo` is passed (some machines keep the home directory itself in git).

**Dedupe.** The dedupe key is `import_key = "<parser>:<native_session_id>"`.

| Situation | Result |
|---|---|
| Same key, same hash | Skip |
| Same key, different hash (the session grew) | New receipt with `import.supersedes = <old receipt_id>`, unless `--no-update` is passed. The old receipt stays. |
| Key matches a live hook record (`capture.session_id` + agent) | Append an **attachment** to the live receipt. No duplicate receipt. |

The index at `.openshard/imports/index.jsonl` is a cache and can be rebuilt from the `import` blocks in `runs.jsonl`.

## 5. Jobs

Imports can run for minutes or hours, so they are resumable jobs.

**Job states:**

```
created → discovering → processing → finalizing → completed
               │             │
               └──► paused ◄─┘   (Ctrl-C, pause request, crash ⇒ resumable)
               └──► cancelled    └──► failed (fatal: auth revoked, repo missing)
```

**Item states:** `pending → fetched → parsed → written | skipped_duplicate | skipped_filtered | failed_retryable(n) | quarantined`

**Job directory: `.openshard/imports/<job_id>/`**
- `job.json`: spec, state, counters; updated by atomic replace.
- `items.jsonl`: append-only, fsync'd item transitions.
- `cursor.json`: the connector's discovery cursor.
- It contains **no content**: only locators, hashes, states, and error classes.

**Checkpointing.** An item's `written` line is appended after its receipt append succeeds. If a crash happens between the two, resume detects the existing receipt through the dedupe index, so the write happens exactly once.

**Retries.**
- Connector IO errors: exponential backoff (1 s → 60 s, max 5 attempts).
- Parser errors: permanent. The item is quarantined as `{object_id, locator_hash, source_sha256, error_class, parser@version}`, again with no content.

**Cancellation.** `ingest cancel <job>` writes a request marker. The runner checks it between items and every N records inside a long stream.

**Background.**
- v1 runs in the foreground with a progress bar and resumes after Ctrl-C.
- `--detach` can reuse the capture service's detached-spawn helper (including the Windows console-detach handling).

**Concurrency.** One active job per repo (`imports/active.lock`). Items are processed sequentially in v1.

## 6. Git and CI evidence

Historical git evidence uses **commit-graph queries only**. It never diffs the current working tree.

1. **Head at start.**
   - The Codex `session_meta.git.commit_hash` is used as-is (`imported_transcript`).
   - Claude records only `gitBranch`, so the head is inferred with `git rev-list -1 --before=<start> <branch>` → `git_observed`.
2. **Commits made by the session.** A SHA qualifies as `git_verified` (and `artifact_sha` is set) when:
   - it appears in the session's tool output (e.g. `git commit` output);
   - `cat-file -e` succeeds;
   - its diff overlaps the files the session edited.
3. **Candidate commits.** Commits in `[start, end + 2h]` on the branch that touch the edited files → `git_observed` only, never verified.
4. **Changed files.** These come from the transcript's edit/write tool calls (`imported_transcript`). Files that intersect a verified commit's diff are upgraded to `git_verified`.
5. **Unverified claims.** A claim like "I committed abc123" whose SHA is missing from the repo stays `agent_reported`.
6. **New git helpers.** Read-only helpers in `util/git.py` via `run_git`: `commit_exists`, `is_ancestor`, `commit_files`, `commits_in_window`.

**CI (later milestone).** Check runs or statuses for verified commits, fetched through GitHub → `independently_verified` / `MODE_CI_REPORT` with `artifact_sha`.

**Pre-seal vs post-seal.**
- Evidence available at import time (local git) goes into the receipt before it is sealed.
- Anything that arrives later goes into attachments.

## 7. Immutability, enrichment, assessments

**Sealed historical receipt.** A `runs.jsonl` line written once, with `sealed_at` and `content_hash`. It is never upserted, amended, or rewritten.

**Attachments** *(proposed store: `.openshard/attachments.jsonl`, append-only)*:

```jsonc
{ "attachment_id": "att_…", "receipt_id": "rcpt_…",
  "pins_content_hash": "sha256:…",
  "kind": "git_verification | ci_result | github_pr | reimport_note | regrouping | manual_note",
  "facts": [{"field": "commit", "value": "abc123…", "evidence": "git_verified", "ref": "git:cat-file"}],
  "produced_by": "enricher.git@1", "produced_at": "…", "supersedes": null,
  "content_hash": "sha256:…" }
```

This sidecar pattern matches Verification v2's `.openshard/verifications.jsonl`. Whether attachments **extend that sidecar** or live beside it is an open decision (§12).

**Assessments.**
- `assess(receipt, attachments, assessor_version)` computes verification status, trust score, and evidence coverage per fact category, at read time.
- Snapshots are optional *(proposed `assessments.jsonl`)*: `{receipt_id, assessor_version, inputs: [content_hash, attachment_ids…], result}`. They let the UI show "v1 assessment → v2 with CI" without touching the receipt.

**Re-import of a grown source.** This produces a new receipt with `supersedes`. The old receipt remains and is marked superseded at read time.

**Regrouping.** A regrouping (combining sessions into one Shard) is recorded as a `regrouping` attachment. It never rewrites `shard_id` on sealed receipts.

## 8. Record shape *(proposed; schema version to be decided after Verification v2)*

The historical receipt reuses the existing record shape, so `build_shard_receipt`, `views.py`, MCP, and sync keep working. It adds two blocks:

```jsonc
{
  "receipt_id": "rcpt_…", "shard_id": "shard-…", "attempt_number": 1,
  "executor": "claude_code_history_import",     // one per parser; AgentProfile + _EXTERNAL_AGENT_LABELS
  "origin": "historical_import",
  "timestamp": "<session start>",
  "task": "<scrubbed excerpt>", "model": "…", "provider": "…",
  "files_changed": [...], "events": [...], "verification": {...},
  "tokens_*": ..., "tokens_provenance": "imported_transcript",
  "estimated_cost": 1.23, "cost_provenance": "estimated_from_tokens",
  "import": {
    "import_key": "claude_code:<native_session_id>",
    "source_sha256": "…",
    "source": {"connector": "local_agent_history", "parser": "claude_code_jsonl@1",
               "locator_hash": "…", "locator_display": "~/.claude/projects/…"},
    "job_id": "ijob_…", "imported_at": "…", "importer_version": "…",
    "session_window": {"start": "…", "end": "…"},
    "grouping": {"rule": "one_session_default", "evidence": "imported_transcript"},
    "supersedes": null
  },
  "facts": {
    "repo":   {"value": "github.com/o/r", "evidence": "imported_transcript", "ref": "L1"},
    "branch": {"value": "main", "evidence": "imported_transcript", "ref": "L1"},
    "head_at_start": {"value": null, "evidence": "unknown"},
    "approvals": {"value": null, "evidence": "unknown"}
  },
  "sealed_at": "…", "content_hash": "sha256:…"
}
```

**Re-verification.** `ref` is a line or record locator inside the source. Together with `source_sha256`, anyone holding the original file can re-verify each fact.

**What stays local.** `locator_display` is home-relative and local-only. It is excluded from `views.py` projections and from sync; only `locator_hash` leaves the machine.

## 9. Interfaces

### CLI (proposed `openshard ingest` group, under "Integrations")

| Command | Purpose |
|---|---|
| `ingest sources [--json]` | Detect local sources, e.g. "Claude Code: 412 sessions, 38 in this repo" |
| `ingest scan <source…> [--since] [--until] [--all-repos] [--json]` | Dry run of discovery and parsing. Shows counts, per-fact evidence coverage, and duplicates. Writes nothing. |
| `ingest run <source…> [--since] [--no-update] [--enrich git] [--detach] [--json]` | Create a job and run it |
| `ingest status / list / resume / cancel` | Job control |
| `ingest enrich --with git [--receipt …\|--job …]` | Post-seal enrichment, written as attachments |

`import claude` and `wrap claude` are unchanged.

### Python API

`openshard.ingest.run_job(spec, *, repo_path, progress_cb) -> JobResult`. The CLI calls it, and so will any future hosted worker.

### Read side

- `get_receipt` / `history --json` / MCP gain `origin`, a `facts` projection (value + evidence, no locators), and an attachments summary.
- A merged assessment is computed at read time.

### Sync

A contract change is needed for `origin`, `import` (key, source hash, parser, job id, locator hash only), `facts`, and an attachments endpoint. This is **deferred**: it must be designed together with the platform `packages/contracts` after Verification v2 lands.

## 10. Local vs hosted

| Concern | Local (Core) | Hosted (Platform, later) |
|---|---|---|
| Local agent history, local files | Only here; the data never has to leave the machine | Shows the results via sync |
| Upload, Drive, Dropbox, S3 | Not in the first milestone (the user downloads the file and imports it by path, once file-drop exists) | Blob connectors in a Python worker running `openshard.ingest`, with transient bytes only. **Not being built yet**; the connector interface is designed for them (opaque cursors, streaming `open`, `check_access`, etag-based stat). |
| GitHub / CI | `gh`/PAT enrichment (later milestone) | GitHub App enrichment |
| Canonical store | `runs.jsonl` + attachment sidecar | Rows keyed by `(org, receipt_id)` + an attachments table |
| Jobs | `.openshard/imports/` | Job table + queue with the same state machine and item-log schema |

## 11. Security model

- **Consent.** Transcripts are read only when the user runs an ingest command. `scan` lists exactly which files will be read.
- **No raw persistence** (invariant 1).
  - `SHARD_BLOCKED_FIELDS` remains as a backstop.
  - A test seeds fixtures with fake secrets and prompt text, then greps all of `.openshard/**` and every sync envelope to check they are absent.
- **Scrub at the normalize boundary.**
  - Every string is scrubbed.
  - Paths are made repo-relative.
  - Absolute and home paths are hashed or kept local-only.
- **Transcript content is data.** Nothing from a transcript is executed. Git queries use fixed argv via `run_git`.
- **Least privilege.**
  - Connectors are read-only: GitHub `contents:read` + `checks:read`, S3 `GetObject`/`ListBucket` on a prefix, Drive `drive.readonly` or a file-picker scope.
  - Local tokens are stored 0600 under `~/.openshard/`, following `sync/config.py`.
  - Hosted tokens are encrypted per org and revocable. A revoked token fails the job and never silently degrades it.
- **Resource limits.**
  - Streaming parse, with per-line length caps and a maximum object size.
  - Archive limits on entry count, total size, path traversal, and nesting depth.
- **Integrity.**
  - Each receipt carries `source_sha256` and `content_hash`.
  - Attachments pin the hash of the receipt they refer to.
  - Signatures remain out of scope, consistent with `docs/platform-sync.md`.

## 12. Milestones

**First milestone (narrow):** foundation → Claude Code history → Codex history → Git evidence.

| # | Branch | Scope |
|---|---|---|
| 1 | `feat/ingest-foundation` | Includes: `openshard/ingest/` model; connector and parser protocols; registry; JobStore; dedupe index; ReceiptBuilder (append-only + seal); a fake connector and parser for tests. Adds the vocabulary and origin agreed after Verification v2 review. |
| 2 | `feat/ingest-claude-code` | Includes: `local_agent_history` connector (`~/.claude/projects/<slug>/<session>.jsonl`); `claude_code_jsonl` parser; normalizer; `ingest sources/scan/run/status/resume/cancel`; repo routing and home guard; live-capture collision handled as an attachment; `AgentProfile` entry. |
| 3 | `feat/ingest-codex` | `codex_rollout` parser (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`: `session_meta.git`, `turn_context` model/approval policy, `function_call`, `token_count`) |
| 4 | `feat/ingest-git-evidence` | Includes: `enrich/git_local.py` and the `util/git.py` helpers (the first `git_verified` producer); the attachment store; read-time merge and `assess()` v1; the `facts` projection in `views.py`; `history --origin historical`. |

**Later (not scheduled):**
- `file_drop` connector (files, folders, zip; sniff autodetect; `--detach`)
- GitHub/CI enrichment (`independently_verified`)
- Sync contract change (Core + Platform)
- Hosted upload and job UI
- Drive/Dropbox/S3 blob connectors
- Cursor (`state.vscdb`), OpenCode, Hermes, and Grok parsers
- An MCP-backed connector

## 13. UI workflow

**CLI.**
1. `ingest sources` shows what is available.
2. `ingest scan claude-code` shows a table: sessions per repo, date range, duplicates, and evidence coverage. Example: "model 100% transcript · test outcomes agent-reported · commits 40% git-verified · cost estimated".
3. `ingest run` shows a progress bar (written / skipped / quarantined) and resumes after Ctrl-C.
4. The summary points to `history --origin historical`.

**Receipt view.**
- A "Reconstructed from history · <source> · imported <date>" banner.
- An evidence chip on every fact: transcript, agent claim, git observed, git verified, CI, or unknown.
- An attachments section listing later evidence with its dates.

**Hosted (later).**
- An "Import history" page with source cards: Upload, Run local importer, GitHub, Drive/S3/Dropbox.
- A job list with progress, pause/cancel, and a quarantine list showing error class and locator hash only.
- An assessment-history toggle.

## 14. Tests (for the first milestone)

Tests follow the `tests/test_grok_bot_capture.py` structure (Normalize / Ingest / Cli classes) and use the `tests/capture_fixtures.py` repo fixture. Fixtures are small synthetic transcripts under `tests/fixtures/ingest/{claude_code,codex}/`, seeded with fake secrets and prompt text.

- **Parsers**
  - Golden fixture → `HistoricalSession` output.
  - Unknown record types become `losses`.
  - Malformed or truncated lines are tolerated.
  - No parser claims another format during sniffing.
- **Evidence**
  - Every fact's evidence is in the allowed set.
  - `directly_observed` is never emitted.
  - Transcript command outcomes are `agent_reported`.
  - Cost is `estimated` or absent.
- **Privacy**
  - After an import, fake secrets, prompt text, and absolute paths are absent from all of `.openshard/**` and from the projections.
- **Immutability**
  - Receipt line bytes are unchanged after enrichment, re-import, and resume.
  - `verify_shard_hash == valid`.
  - Attachments pin the correct hash.
- **Idempotence**
  - Running twice produces no new records.
  - A grown source produces a new receipt with `supersedes`.
  - A session already captured by hooks produces an attachment, not a duplicate.
- **Jobs**
  - Inject a crash after N items; resume writes each item exactly once.
  - Cancel is honoured.
  - A transient connector error is retried.
  - A parse error is quarantined with no content.
  - The active-job lock prevents a second job.
- **Routing**
  - cwd → correct repo.
  - A home-dir git repo is refused.
  - `--all-repos` skips repos without `.openshard/`.
- **Git** (real repo with backdated commits via `GIT_AUTHOR_DATE` / `GIT_COMMITTER_DATE`)
  - A SHA in tool output → `git_verified`.
  - A commit found only by time window → `git_observed`.
  - A missing SHA stays `agent_reported`.
  - The working tree is never diffed.
- **CLI**
  - `CliRunner` over `sources/scan/run/status/cancel --json`, using a fake home containing `.claude/projects` and `.codex/sessions`.
- **Scale smoke test**
  - Thousands of synthetic sessions stream within a memory bound.
  - Time budgets stay loose (shared CI runners are noisy).

## 15. Decisions still open before implementation

1. **Evidence vocabulary.** Is `imported_transcript` a new rung on the evidence ladder, or a separate *channel* field next to the existing ladder? And does it rank above or equal to `agent_reported` for invocation facts? This depends on how Verification v2 finalizes the source/mode split.
2. **Attachments store.** Should attachments extend Verification v2's `.openshard/verifications.jsonl` sidecar into a general "evidence sidecar", or stay a separate `attachments.jsonl` with a shared read-time merge?
3. **Schema version.** Should `import`/`facts` bump the record schema, and to what number? This has to be sequenced with Verification v2 and any in-flight model/routing record changes.
4. **Origin and executor naming.** Choose between `origin=historical_import` and a flag on `external_observed`. Choose between one executor per parser (`claude_code_history_import`, `codex_history_import`) and a single `history_import` executor plus an agent field.
5. **Model and cost.** How should model ids normalize against the dynamic model catalog (PR #347)? Which pricing table and version should back `estimated` cost, and should historical cost be shown at all in v1?
6. **Superseding grown sources.** Should a grown session produce a new receipt (the current proposal), or only an attachment that points at the newer source hash?
7. **Grouping evidence threshold.** What counts as "strong evidence" before the grouping policy may combine sessions into one Shard beyond the v1 default? One-session-per-Shard stays the default until this is decided.
8. **Sync.** When do historical receipts become sync-eligible? What is the contract shape for `origin`/`import`/`facts` and attachments? Should hosted orgs be able to opt out of receiving historical receipts?
9. **Retention of source hashes.** Is `source_sha256` plus `locator_hash` enough for re-verification, or should the user be able to register an archive location (never copied) for later audit?

## 16. v1 implementation notes

### How the §15 decisions were settled for v1

| # | Decision | v1 choice |
|---|---|---|
| 1 | Evidence vocabulary | `imported_transcript` and `git_verified` are new values on the existing Event evidence ladder (`history/event.py` `VALID_EVIDENCE`). They are not a separate channel field. Verification blocks use the new `observation_mode = imported_transcript`. Transcript-recorded outcomes use `source = agent_reported`. An invocation with no recorded outcome has `source = null` and status `unknown`. |
| 2 | Attachments store | A separate append-only `.openshard/attachments.jsonl`, next to Verification v2's `verifications.jsonl`. It is not merged into that sidecar. |
| 3 | Schema version | Not bumped. `import`, `facts`, `origin` and `sealed_at` are additive keys, and `coerce_shard_entry` passes them through. |
| 4 | Origin / executor | `origin = historical_import` (`history/shard.py`, capture depth `partial`), with one executor per parser: `claude_code_history_import` and `codex_history_import`. |
| 5 | Model and cost | Model ids are stored as the transcript records them (scrubbed), with no catalog normalization. Cost is not produced in v1: `facts.cost` is `unknown`. Tokens are kept, with `tokens_provenance = imported_transcript`. |
| 6 | Grown sources | A grown source produces a new receipt with `import.supersedes`, as proposed. `--no-update` skips it. |
| 7 | Grouping | One session → one Shard, recorded as `import.grouping`. |
| 8 | Sync | Historical receipts are **not sync-eligible** (`sync/envelope.py` reason `historical_import_sync_deferred`) until the contract gains `origin`/`import`/`facts`. |
| 9 | Retention | Only `source_sha256` and `locator_hash` are kept. |

### Differences from the design above

- **`locator_display` is `<source>:<file name>`** (e.g. `claude-code:<session>.jsonl`), not a home-relative path, because Claude Code project directory names encode the absolute project path. Job item logs store `object_key` (a hash of the object id) instead of the locator.
- **Parse losses and deliberate drops are recorded separately.** Unreadable, malformed or unknown source records go to `import.source_losses`, and to the receipt's known gaps as `unparsed_source_records`. Facts OpenShard chose not to keep go to `import.dropped` (e.g. `path_outside_repo`).
- **Claude `tool_result.is_error` omitted = success.** The Messages API defines `is_error` as optional with default false, and Claude Code omits it on many successful results. `interrupted` or a background task id means `not_completed`, which resolves to `unknown`.
- **Codex prompts.** Newer rollouts have no `user_message` event. The first `response_item` user message that is not Codex-injected context (`<environment_context>`, `AGENTS.md`) is used instead.
- **Subagent transcripts.** Sidechain records inside a session file fold into the session. Separate `<session>/subagents/*.jsonl` files are not read yet.
- **Sealed receipts refuse amendment.** `history.store.amend_latest_record` raises `SealedReceiptError` for a record with `sealed_at`, so `openshard note` and `feedback` fail with a clear message instead of rewriting an imported receipt.
- **Defensive seal check.** `receipt_builder.assert_no_live_evidence` refuses to write any record or attachment whose `evidence`, `source` or `observation_mode` is `directly_observed` or `openshard_executed`.

### Deferred from the first milestone

- `ingest enrich` (post-seal attachments from later git or CI evidence)
- the read-time `assess()` merge and the `facts` projection in `views.py` / MCP
- `history --origin historical`
- `--detach`
- CI/GitHub evidence (`independently_verified`)
- the sync contract change
- all hosted/blob connectors (upload, Drive, Dropbox, S3)
- Cursor, OpenCode, Hermes and Grok parsers
