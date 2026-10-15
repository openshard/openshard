# Cross-system workflow correlation

This implements the capture and ingestion boundary for **Next Order of Business (3 October
2026)**, against Core `eba20f7` and Platform `708e912`. It does not claim that
the complete trigger-to-production workflow has been demonstrated.

## Contract

New OSN/pipeline and `wrap claude` records can carry a bounded `correlation`
block, captured from the explicitly supplied `OPENSHARD_CORRELATION_CONTEXT`
JSON environment variable before sealing the record. Adapters can use
`history.correlation.correlation_block` as a generic normalisation boundary.

```json
{
  "parent_run_id": "parent-run-1",
  "trigger": "github.issue",
  "source": "github",
  "traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
  "external_ids": [
    {"namespace": "github.workflow", "id": "openshard/openshard/123"},
    {"namespace": "deployment", "id": "deployment-123"}
  ]
}
```

The stored block always says `evidence: declared`. A deployment ID is not
proof of deployment, health, approval or authority. No outcome is inferred
from links or trace IDs. Verification must still bind to the exact artifact.

IDs are capped at 256 characters and never truncated; there are at most 16
namespaced external IDs. Invalid/unsafe fields are dropped. The JSON launch
context is limited to 8192 UTF-8 bytes. Arbitrary payloads, baggage and
tracestate are not copied. This initial boundary validates W3C version 00
traceparent only; it is not an OpenTelemetry exporter or trace propagator.
Reference: https://www.w3.org/TR/trace-context/

## Identity and local inspection

Existing `shard_id`, `run_id`, `receipt_id` and `task_id` semantics are
unchanged. A trace does not become a Shard. The current globally unique,
explicit cross-run identity is `task_id`; local Shard labels are not globally
unique. Existing explicit same-Shard attempts remain supported.

Create a task with `openshard task new`, then pass its ID through the existing
`--task-id` option when starting work. Supply the correlation JSON in the
launch environment. Inspect linked local records with:

```sh
openshard workflow timeline --task-id task_018f4d2a-1c3e-7000-8b1a-0242ac120002
```

This read-only JSON view lists each matching run and Receipt in timestamp
order, retaining its own IDs and declared links. It never groups by prompt,
time proximity, trace ID or shard-label equality. It does not rewrite history.

Extended machine Receipts include correlation only when recorded. Older
Receipts and the existing default MCP response shape remain unchanged.
Deploy the paired Platform contract update **before** sending correlated
Receipts: an older strict receiver correctly rejects unknown fields.

## Verification and work log

- 127 focused Core tests passed, including new privacy, malformed-context,
  legacy-read, identity, timeline and actual wrap-producer tests.
- Repository-wide Ruff and mypy passed (297 source files at this checkpoint).
- 61 Platform contract tests passed, including new backwards-compatibility
  and trace-validation cases; JSON schema regenerated.
- Tests use isolated temporary data. They are not production dogfood.

## Expanded implementation (4 October 2026)

- Launch context now crosses the authenticated Claude capture header, reduced
  queue, replay and neutral adapter path. First binding is frozen and later
  conflicts are counted. Raw agent payloads cannot declare launch context.
- Platform PR 86 adds an authenticated, idempotent append-only workflow-event
  boundary and hosted task timeline. Reports retain source_reported authority;
  neither matching commit IDs nor deployment success makes verification green.
- Claude hooks capture SessionStart.model, PostModelSwitch.to_model and effective
  effort.level. No model is inferred from ANTHROPIC_MODEL or user statements.
  This needs the host to deliver those fields; older clients still leave unknowns.
- Core 0.4.10 is prepared with an isolated installed-wheel smoke in the release
  workflow. Built local wheel remote create/attach help checks and twine passed.
  This is not a claim that 0.4.10 has been published.
- `openshard verify --compare-base COMMIT` adds local baseline diagnostics. See
  [baseline-verification.md](baseline-verification.md) for scope and limitations.

## Remaining delivery gates

- Live issue/trigger → agent → commit → PR → exact-commit CI → deployment
  → production-health dogfood, with actual evidence from every source.
- A deliberate global Shard identity migration if required; do not silently
  reinterpret existing local labels.

This work does not implement onboarding, billing, WorkOS/Resend, new policy
enforcement or deeper OSN orchestration. Those remain separate roadmap items.

## Herdr

[Herdr](https://herdr.dev) runs each agent CLI in a terminal pane and exports
`HERDR_ENV=1`, `HERDR_WORKSPACE_ID`, `HERDR_PANE_ID` and `HERDR_BIN_PATH` to
processes in it. OpenShard turns that into **declared** `correlation` links
(`evidence: declared`, no new Receipt fields):

```json
{"evidence": "declared", "source": "herdr", "external_ids": [
  {"namespace": "herdr.terminal", "id": "term_65dd088a69b528"},
  {"namespace": "herdr.pane", "id": "w5/p2"},
  {"namespace": "herdr.delegated_session", "id": "ses_0123456789abcdef0123456789abcdef"}]}
```

- **Launch context** (`herdr.terminal`, `herdr.pane`, `source: herdr`) is read
  where `OPENSHARD_CORRELATION_CONTEXT` is read: the command-hook processes
  (Claude `SessionStart`, Codex, Cursor, Antigravity, OpenCode, ...), `wrap` and
  `run`. One `herdr agent get <pane>` call with a 2 s timeout resolves the
  terminal id (cached per pane within the process). If Herdr is absent, slow or
  returns garbage, the terminal id is skipped and capture is unaffected.
  Claude's HTTP hooks carry no environment; the launch context reaches them
  through the existing authenticated capture header.
- An explicit `OPENSHARD_CORRELATION_CONTEXT` always wins. Herdr only supplies a
  missing `source` and appends its ids while the 16-id cap has room.
- **Delegation edges.** When a captured shell command (Claude `Bash`,
  Codex/others whose command hooks expose it) successfully runs
  `herdr agent prompt|send-keys|start <target>`, the target is resolved with
  `agent get` and the **child's `session_key`** is recorded on the **parent**
  Receipt as `herdr.delegated_session`. `get/list/read/wait` are ignored. If
  resolution fails, or the Herdr agent has no Core capture adapter, nothing is
  recorded. Order is first-seen; ids beyond the 16 cap are dropped (they are
  appended last, so launch and explicit ids are never displaced).
- **Privacy.** The agent's raw session id never leaves the machine: the id is
  `ses_` + sha256(`<capture agent>:<session id>`)[:32], the same function that
  produces `session.session_key`. Only ids are copied: no prompt text, cwd or
  terminal output.

Platform matching rule: a parent Receipt's `herdr.delegated_session` id equals
the child Receipt's `session.session_key`. `herdr.terminal` groups Receipts
captured in one terminal.

Limitations: workspace and pane ids are reused across Herdr restarts, so
`herdr.pane` is a hint, not an identity (`herdr.terminal` and the session key
are the strong identifiers). A delegation is seen only if the orchestrator's
shell command is captured. A Herdr-run agent without a Core capture adapter
has no child Receipt to link. Cost and tokens stay on each agent's own Receipt;
Core never sums across a delegation, which would double count.
`openshard workflow timeline --task-id ID --herdr-terminal TERM` shows these ids
and can filter by terminal.
