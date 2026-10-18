# Historical Ingestion

## 1. Overview

`openshard ingest` (`openshard/ingest/`) imports past Claude Code and Codex sessions from local agent history as receipts, with local git evidence. The receipts are labelled "Reconstructed from history":
- every fact states where it came from;
- OpenShard never claims it observed anything live;
- sealed receipts never change.

Live capture never imports past sessions. It may read the current session's `transcript_path` for bounded usage metadata, but never copies transcript content or the locator into the Receipt. `SHARD_BLOCKED_FIELDS` strips `transcript`, `raw_prompt` and similar fields (`history/shard_schema.py`).

## 2. Invariants

These are permanent and enforced by tests.

1. **No raw transcript persistence.** Transcripts and other source bytes are streamed and processed in memory. Nothing raw is written to `.openshard/`, job files, logs, telemetry, sync payloads, or hosted storage. Only reduced, scrubbed facts, a SHA-256 of the source bytes, and a locator are kept. Transient processing is fine; persistence is not.
2. **Never `directly_observed`.** Historical ingestion never emits `directly_observed` (or `openshard_executed`) evidence. Imported evidence always ranks below what OpenShard observed live.
3. **Every fact has a provenance.** `Fact = {value, evidence, source_ref}`.
   - A fact that cannot be recovered is recorded as `unknown` / not observable.
   - It is never silently omitted and never guessed.
4. **Sealed receipts are immutable.** Historical receipts are append-only writes, sealed with `content_hash`, and never upserted.
5. **Enrichment is append-only.** Later evidence is stored as attachments that pin `(receipt_id, content_hash)`.
6. **Idempotent.** Re-running any job is a no-op unless the source bytes changed.
7. **A connector is not a parser.**
   - A connector knows *where* the bytes live.
   - A parser knows *what format* they are in.
8. **One parser implementation.** Parsers are written in Python, in Core.

### Defaults

- **One native session = one Shard.** Grouping is recorded as a fact with its own evidence (`import.grouping: {rule, evidence}`). Claude sidechain/subagent records inside a session file fold into their parent session.
- **Current-repo-only routing** (see §4).
- **30-minute quiescence window.** Sources modified more recently than this are skipped because they may still be live.

## 3. Evidence model

`imported_transcript` and `git_verified` are values on the existing Event evidence ladder (`history/event.py` `VALID_EVIDENCE`).

| Evidence | Meaning | Typical historical facts |
|---|---|---|
| `imported_transcript` | The agent host's own log recorded this at the time, and OpenShard read it later. **Below `directly_observed`.** | a tool or command was invoked; timestamps; model id; token usage; the cwd and branch recorded by the agent |
| `agent_reported` | Claims or outcomes asserted by the agent or its host | assistant prose ("tests pass"); transcript-recorded **command outcomes** (see below) |
| `git_observed` | Found in git, but the link to this session is not certain | commits in the session window that touch the edited files |
| `git_verified` | The git object exists and is strongly linked to the session | a SHA that appears in the session's tool output, exists (`cat-file -e`), is reachable from the branch, and whose diff overlaps the session's edits |
| `unknown` | Not observable from the available evidence | approvals when the log does not record them; cost |

**Alignment with Verification v2.** Verification v2 classifies every hook-reported command outcome as `agent_reported`, and only `openshard verify` yields `directly_observed`. A pass/fail outcome read from a transcript (e.g. Claude `tool_result.is_error`, Codex `function_call_output`) is also `agent_reported`. Only the fact that the invocation *happened* is `imported_transcript`. Verification blocks use `observation_mode = imported_transcript`. An invocation with no recorded outcome has `source = null` and status `unknown`, and an ambiguous status (not completed, or a conflict) resolves to `unknown`.

**Origin.** `origin = historical_import` (`history/shard.py`), with `capture_depth` always `partial`. There is one executor per parser: `claude_code_history_import` and `codex_history_import`.

**Model and cost.** Model ids are stored as the transcript records them (scrubbed), with no catalog normalization. Cost is not produced: `facts.cost` is `unknown`. Tokens are kept, with `tokens_provenance = imported_transcript`.

## 4. Pipeline

```
SourceConnector.discover() ──► SourceObject stream
  └─► Parser.sniff()/parse() ──► ParsedSession (format-specific, in memory, transient)
       └─► Normalizer ──► HistoricalSession (neutral IR; every field a Fact; scrubbed)
            └─► Enrichers (local git) ──► Facts (pre-seal) | Attachments (post-seal)
                 └─► Deduper (import_key + source_sha256; live-capture collision)
                      └─► ShardBuilder (grouping policy + repo routing)
                           └─► ReceiptBuilder (coerce, receipt_id, events, seal, append)
JobRunner drives each item with checkpoints, retries, and cancellation.
```

| Module | Responsibility |
|---|---|
| `connectors/base.py` | `SourceConnector` protocol: `kind`, `describe()`, `check_access() -> AccessStatus`, `discover(cursor, filters) -> Iterator[SourceObject]`, `open(obj) -> BinaryIO` (streaming), `stat(obj)`. `SourceObject{connector, object_id (stable), locator, size, mtime, etag, hint}`. The cursor is opaque. |
| `connectors/local_agent_history.py` | Reads `~/.claude/projects/<slug>/<session>.jsonl` and `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` |
| `parsers/base.py` | `Parser` protocol: `name`, `version`, `sniff(head_bytes, obj) -> float`, `parse(stream, obj) -> Iterator[ParsedSession]`. Parsers are pure, with no IO beyond the stream. Unknown record types are counted as `losses` and never crash the parse. |
| `parsers/claude_code.py`, `parsers/codex.py` | `claude_code_jsonl` and `codex_rollout` (`session_meta.git`, `turn_context` model/approval policy, `function_call`, `token_count`) |
| `model.py` | `Fact`, `SourceRef{source_sha256, ref}`, `HistoricalSession{native_session_id, agent, window, cwd, repo_hint, branch, head, task, model, provider, tokens, tool_calls, commands, file_edits, test_runs, approvals, claims, losses}` |
| `normalize.py` | Converts a `ParsedSession` into a `HistoricalSession`. **This is the scrub boundary**: `scrub_text_for_secrets` on every string, and `sanitize_path` to make paths repo-relative. It reuses the hook command reducer (`command_kind` test/lint/other) and `task_title`. |
| `enrich/git_local.py` | Historical git evidence (§6) |
| `dedupe.py` | Checks `import_key` + `source_sha256`, and detects collisions with live capture |
| `shard_builder.py` | Grouping policy and repo routing |
| `receipt_builder.py` | `coerce_shard_entry` → `ensure_receipt_id` → embedded events → seal → `append_jsonl` under `history_file_lock`. **It never calls `upsert_jsonl`.** |
| `jobs.py` | State machine, checkpoints, retries, cancellation |
| `store.py` | `HistoryStore` and `JobStore` protocols and their local JSONL/filesystem implementations |
| `registry.py` | Connector and parser registry. New entries do not touch the pipeline. |

**Storage neutrality.** The pipeline talks to three small protocols:
- `HistoryStore`: append receipt, append attachment, look up an `import_key`
- `JobStore`: job and item log
- `SourceConnector`

No connector or parser imports a storage module.

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

**Item states:** `pending → fetched → parsed → written | attached | skipped_duplicate | skipped_filtered | failed_retryable(n) | quarantined`

**Job directory: `.openshard/imports/<job_id>/`**
- `job.json`: spec, state, counters; updated by atomic replace.
- `items.jsonl`: append-only, fsync'd item transitions.
- `cursor.json`: the connector's discovery cursor.
- It contains **no content**: only object keys, hashes, states, and error classes.

**Checkpointing.** An item's `written` line is appended after its receipt append succeeds. If a crash happens between the two, resume detects the existing receipt through the dedupe index, so the write happens exactly once.

**Retries.**
- Connector IO errors: exponential backoff (1 s → 60 s, max 5 attempts).
- Parser errors: permanent. The item is quarantined as `{object_id, locator_hash, source_sha256, error_class, parser@version}`, again with no content.

**Cancellation.** `ingest cancel <job>` writes a request marker. The runner checks it between items and every N records inside a long stream.

**Foreground.** A job runs in the foreground with progress output and resumes after Ctrl-C.

**Concurrency.** One active job per repo (`imports/active.lock`). Items are processed sequentially.

## 6. Git evidence

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
6. **Git helpers.** Read-only helpers in `util/git.py` via `run_git`: `commit_exists`, `is_ancestor`, `commit_files`, `commits_in_window`.

**Pre-seal vs post-seal.**
- Evidence available at import time (local git) goes into the receipt before it is sealed.
- Evidence attached to an existing receipt goes into attachments.

## 7. Immutability and attachments

**Sealed historical receipt.** A `runs.jsonl` line written once, with `sealed_at` and `content_hash`. It is never upserted, amended, or rewritten. `history.store.amend_latest_record` raises `SealedReceiptError` for a record with `sealed_at`, so `openshard note` and `feedback` fail with a clear message instead of rewriting an imported receipt.

**Attachments** (`.openshard/attachments.jsonl`, append-only, separate from Verification v2's `verifications.jsonl`). A session already captured live gets a `reimport_note` attachment carrying the imported facts, pinned to the live receipt's hash:

```jsonc
{ "attachment_id": "att_…", "receipt_id": "rcpt_…",
  "pins_content_hash": "sha256:…",
  "kind": "reimport_note",
  "facts": [{"field": "branch", "value": "main", "evidence": "imported_transcript", "ref": "L1"}],
  "produced_by": "claude_code_jsonl@1+historical-ingestion@1", "produced_at": "…", "supersedes": null,
  "import_key": "claude_code:<native_session_id>", "source_sha256": "…", "source": {…}, "job_id": "ijob_…",
  "content_hash": "sha256:…" }
```

**Re-import of a grown source.** This produces a new receipt with `supersedes`. The old receipt remains.

**Defensive seal check.** `receipt_builder.assert_no_live_evidence` refuses to write any record or attachment whose `evidence`, `source` or `observation_mode` is `directly_observed` or `openshard_executed`.

## 8. Record shape

The historical receipt reuses the existing record shape, so `build_shard_receipt`, `views.py`, MCP, and sync keep working. The schema version is not bumped: `import`, `facts`, `origin` and `sealed_at` are additive keys, and `coerce_shard_entry` passes them through.

```jsonc
{
  "receipt_id": "rcpt_…", "shard_id": "shard-…", "attempt_number": 1,
  "executor": "claude_code_history_import",     // one per parser; AgentProfile + _EXTERNAL_AGENT_LABELS
  "origin": "historical_import",
  "timestamp": "<session start>",
  "task": "<scrubbed excerpt>", "model": "…", "provider": "…",
  "files_changed": [...], "events": [...], "verification": {...},
  "tokens_*": ..., "tokens_provenance": "imported_transcript",
  "import": {
    "import_key": "claude_code:<native_session_id>",
    "source_sha256": "…",
    "source": {"connector": "local_agent_history", "parser": "claude_code_jsonl@1",
               "locator_hash": "…", "locator_display": "claude-code:<session>.jsonl"},
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

**Re-verification.** `ref` is a line or record locator inside the source. Together with `source_sha256`, anyone holding the original file can re-verify each fact. Only `source_sha256` and `locator_hash` are kept.

**What stays local.** `locator_display` is `<source>:<file name>` (e.g. `claude-code:<session>.jsonl`), not a path, because Claude Code project directory names encode the absolute project path. Job item logs store `object_key` (a hash of the object id) instead of the locator. Only `locator_hash` leaves the machine.

**Losses and drops.** Unreadable, malformed or unknown source records go to `import.source_losses`, and to the receipt's known gaps as `unparsed_source_records`. Facts OpenShard chose not to keep go to `import.dropped` (e.g. `path_outside_repo`).

## 9. Interfaces

### CLI

| Command | Purpose |
|---|---|
| `ingest sources [--json]` | Detect local sources, e.g. "Claude Code: 412 sessions, 38 in this repo" |
| `ingest scan [claude-code\|codex…] [--since] [--until] [--all-repos] [--json]` | Dry run of discovery and parsing. Shows counts, per-fact evidence coverage, and duplicates. Writes nothing. |
| `ingest run [claude-code\|codex…] [--since] [--until] [--all-repos] [--no-update] [--no-enrich] [--json]` | Create a job and run it. Local git evidence is added unless `--no-enrich` is passed. |
| `ingest status / list / resume / cancel` | Job control |

Every command takes `--repo-path` and `--allow-home-repo`. With no source named, both sources are used. `import claude` and `wrap claude` are unchanged.

### Python API

`openshard.ingest.run_job(spec, *, repo_path, progress_cb) -> JobResult`. The CLI calls it.

### Sync

Historical receipts sync through the same privacy-bounded Receipt projection as live runs: `origin = historical_import`, capture completeness, token provenance and structured verification including `imported_transcript`. The raw `import` block, source hashes/locators, `facts` references and the attachment sidecar are not added to the hosted payload; they remain local.

## 10. Security model

- **Consent.** Transcripts are read only when the user runs an ingest command. `scan` lists exactly which files will be read.
- **No raw persistence** (invariant 1).
  - `SHARD_BLOCKED_FIELDS` remains as a backstop.
  - A test seeds fixtures with fake secrets and prompt text, then greps all of `.openshard/**` and every sync envelope to check they are absent.
- **Scrub at the normalize boundary.**
  - Every string is scrubbed.
  - Paths are made repo-relative.
  - Absolute and home paths are hashed or kept local-only.
- **Transcript content is data.** Nothing from a transcript is executed. Git queries use fixed argv via `run_git`.
- **Read-only.** The connector only reads the agent history files.
- **Resource limits.** Streaming parse, with per-line length caps and a maximum object size.
- **Integrity.**
  - Each receipt carries `source_sha256` and `content_hash`.
  - Attachments pin the hash of the receipt they refer to.

## 11. Parser notes

- **Claude `tool_result.is_error` omitted = success.** The Messages API defines `is_error` as optional with default false, and Claude Code omits it on many successful results. `interrupted` or a background task id means `not_completed`, which resolves to `unknown`.
- **Codex prompts.** Newer rollouts have no `user_message` event. The first `response_item` user message that is not Codex-injected context (`<environment_context>`, `AGENTS.md`) is used instead.
- **Subagent transcripts.** Sidechain records inside a session file fold into the session. Separate `<session>/subagents/*.jsonl` files are not read.
