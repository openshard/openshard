# OpenShard architecture

OpenShard is **receipts for AI coding agents**. A Receipt is an evidence
record of one coding-agent session: what was asked, which agent did it,
what changed, what was checked, how completely OpenShard saw it, and what
it cost. The system never claims more than its evidence supports; where
evidence is missing the Receipt says so.

This page is the five-minute map. Everything under "Primary product" is
the receipt path a Staff Engineer should read first; everything under
"Optional and advanced systems" is bolted on beside it and can be ignored
when reasoning about receipts.

## Primary product: the receipt path

```text
External coding agent          Claude Code · Codex · Cursor · OpenCode
        │  hooks / plugin (agent-native mechanisms; fail-open for the agent)
        ▼
Adapters                       adapters/claude_hooks.py (shared fold)
                               adapters/codex_hooks.py, cursor_hooks.py,
                               opencode_plugin.py (translators)
                               adapters/capture_agents.py (identity table)
        │  translate -> HookPayload -> ReducedHookPayload
        │  (scrubbed prompt excerpt, repo-relative path, summarized command;
        │   never transcripts, tool output, file contents, absolute paths)
        ▼
Durable capture                adapters/claude_capture_service.py
                               adapters/claude_capture_client.py
                               adapters/capture_auth.py
        │  authenticated loopback POST -> fsync'd per-session queue -> 200
        │  background replay; undecodable lines quarantined + counted
        ▼
Canonical evidence             history/event.py (Event: evidence level per fact)
                               directly_observed | agent_reported | git_observed
                               | independently_verified | unknown
        ▼
Shard / Receipt record         .openshard/runs.jsonl (one JSON line per record)
                               history/shard_schema.py (coerce, blocked fields)
                               history/shard_hash.py (content hash)
                               history/receipt_identity.py (receipt_id)
                               history/capture_completeness.py
        ▼
Local history + rendering      history/query.py, views.py, shard_contract.py
                               cli: last · history · context · stats · doctor
                               mcp/server.py (read-only tools for agents)
```

### The stages

1. **Agents and adapters.** Each supported agent delivers lifecycle
   events through its own mechanism (Claude Code HTTP + command hooks,
   Codex and Cursor command hooks, an OpenCode plugin). One translator per
   agent turns those into a neutral `HookPayload`; from there a single
   fold (`claude_hooks.py`) is shared. Per-agent facts (labels, executor,
   what a "successful edit" signal is) live in one table
   (`capture_agents.py`), never in branches.
2. **Reduced payloads.** Before anything is persisted outside
   `runs.jsonl`, the payload is reduced to the privacy-safe
   `ReducedHookPayload`: scrubbed 300-char excerpt of the first prompt,
   repo-relative file targets, a 100-char summarized command. This is the
   sanitisation boundary; everything downstream only ever sees this shape.
3. **Durable capture.** The loopback service accepts an authenticated
   POST (per-user token or repository-scoped capability, see
   `docs/agent-capture.md`), appends one fsync'd line to the session's
   queue, and answers. A background worker replays queues through the
   fold. Malformed lines are quarantined and counted, never skipped; a
   transient I/O failure is retried, never treated as corruption. Every
   entrypoint falls back to in-process folding when no service is
   reachable, and the agent itself is never blocked by OpenShard.
4. **Canonical evidence.** Each fact becomes an `Event` with an evidence
   level. The fold never upgrades a level: a hook firing is
   `directly_observed`; a tool call the agent reported is
   `agent_reported`; a repository difference is `git_observed`. Nothing
   is `independently_verified` unless OpenShard ran the check itself.
5. **Shard / Receipt record.** The fold upserts one record per agent
   session into `.openshard/runs.jsonl`. `shard_id` is the history-position
   identity (`shard-YYYYMMDD-NNNN`); `receipt_id` (`rcpt_…`, v0.4.4) is
   the global one. `capture_depth` says how much could be observed and
   `capture.completeness` whether evidence is known to be missing (two
   separate facts). `changes` and `files_detail[].attribution` separate
   agent-reported, git-observed, pre-existing and other-session changes.
   `content_hash` is an unkeyed tamper-evidence hash of the stored record.
6. **Local history and rendering.** `last`, `history`, `context`, `stats`
   and the MCP tools read only this repository's `runs.jsonl`. Rendering
   (`shard_contract.py`) states facts (Capture, Checks, Integrity, Risk as
   recorded) and never turns one fact into another at display time.

### Identity, briefly

| Concept | Field | Meaning | Status |
|---|---|---|---|
| Receipt identity | `receipt_id` | one persisted record, globally unique | v0.4.4 |
| History identity | `shard_id` | position in this repository's history; grouping key for attempts | unchanged |
| Agent session | `capture.session_id` | the agent's own id; per agent | unchanged |
| Task identity | `task_id` | "the same engineering task across attempts, agents and potentially repositories" | v0.4.6: explicit only — minted by `openshard task new`, attached with `--task-id`; never inferred from prompt text, timing, `shard_id` or anything else. See `history/task_identity.py`. |
| Owner / Requested by / Executed by / Approved by | — | accountable person, delegator, performer, approver | **not yet**: only "Executed by" is known (the agent); nothing is inferred from git config or the OS user |

## Optional and advanced systems

These exist beside the receipt path. They are not needed to understand,
trust or use receipts, and the receipt path does not depend on them.

| System | Where | What it is |
|---|---|---|
| Native run pipeline | `run/`, `native/` | OpenShard executing a task itself (planner / executor / validator), producing `origin = openshard_routed` records with real verification. |
| Routing and model registry | `routing/`, `models/` | Model selection and policy for the native pipeline. Advisory for external-agent receipts. |
| OSN (proof pipeline) | `osn/`, `history/proof_contract.py` | Proof-contract sections and status for native runs; surfaces as `Proof:` and `openshard proof`. |
| Trust Score | `history/trust_score.py`, `openshard trust` | A heuristic over recorded proof signals. Diagnostic only; not a receipt signal. |
| Evals | `evals/`, `openshard eval` | Local eval harness for the native pipeline. |
| Workflow packs, review domains | `packs/`, `review/` | Repeatable review prompts for native runs. |
| TUI | `tui/` | Interactive front-end over the same history. |
| Telemetry | `telemetry/` | Privacy-safe counters (`docs/telemetry.md`); never receipt contents. |
| Platform sync | `sync/`, `openshard sync` | Sends copies of the `history --json` projection to a hosted organisation, keyed by `receipt_id` (`docs/platform-sync.md`). The local record stays canonical. |

## Where to read next

* `docs/agent-capture.md` — per-agent capture, trust boundary and
  authentication, change attribution, capture completeness.
* `docs/what-is-a-shard.md` — the Shard / Receipt model for users.
* `docs/telemetry.md` — the complete telemetry contract.
* `docs/platform-sync.md` — what hosted Receipt history sends, when, and
  how it fails.
* `SECURITY.md` — what is in and out of scope, and how to report.
* `docs/architecture/V044_RECEIPT_INTEGRITY_AUDIT.md` and
  `docs/architecture/POST_V044_CORE_CLEANUP.md` — the v0.4.4 audit and
  the next bounded cleanup.
