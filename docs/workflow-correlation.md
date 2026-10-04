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
