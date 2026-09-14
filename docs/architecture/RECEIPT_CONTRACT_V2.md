# Receipt Contract v2

Status: implemented in v0.5 foundation (`openshard/history/receipt_contract.py`).
Contract version `2.0`. Persisted run records stay at `schema_version "1.2"`.

## Why

A v0.4 Shard receipt records that an agent ran and what changed. v0.5 asks
the receipt to be the **evidence record** for the run: enough to answer, for
any run, who owns it, who asked for it, who executed it, what it was allowed
to do, what governed it, who approved it, what was verified and by whom,
whether capture was complete, what it cost to reach a verified result, what
happened afterwards, and whether the record was altered.

A receipt is not a log and not a score. It is a set of explicit facts with
their provenance, from which a **state** is derived by fixed rules.

## Design rules

1. **Additive.** No v0.4.3 field is renamed or reinterpreted. Old records
   build a complete contract with honest gaps.
2. **Read-time projection.** `ReceiptContract` is derived from a persisted
   entry (plus optional sibling attempts) and never written back, exactly
   like `ShardReceipt`.
3. **Never fabricate.** A missing fact is `None` or `"unknown"`. No approver,
   owner, cost or outcome is ever inferred from another field.
4. **Explicit evidence over scores.** The state carries `state_reason` and
   `state_evidence`; the trust score remains a separate consumer.
5. **Bounded and safe.** All strings pass the shared sanitizer (secret and
   absolute-path redaction, length caps). Blocked fields never appear.
6. **Separate principals.** Owner, requested-by, executed-by and approved-by
   are four fields with their own `Principal` (`kind`, `id`, `display`,
   `source`).

## Optional persisted blocks (all top-level, all optional)

Producers that know them write them; 0.4.3 readers ignore them. None is in
`SHARD_BLOCKED_FIELDS`, and `coerce_shard_entry` passes them through.

| Key | Shape | Answers |
| --- | --- | --- |
| `actors` | `{owner, requested_by, executed_by, approved_by}` each a Principal `{kind, id, display, source}` | Q2 owner, Q3 requester, Q4 executor, Q8 approver |
| `permissions` | `{requested: [..], used: [..], denied: [..], source}` (strings like `write:src/**`, `shell:pytest`) | Q5 |
| `policy` | `{policy_id, policy_version, name, source, decision, reason, evaluated_at}` | Q6; `policy_decisions[]` (0.4) still hold the individual decisions |
| `approval` | `{required, status ∈ not_required/pending/granted/denied, approver: Principal, approved_at, mechanism ∈ cli_prompt/auto_policy/dashboard/github_review/api, reason, request_action, request_id}` | Q7, Q8; supersedes but does not replace `approval_request` / `approval_receipt` |
| `verifiers` | list of `{check, status, verifier_kind, verifier_id, independent, duration_seconds, cost_usd, summary}` | Q11, Q15 |
| `escalation` | `{occurred, from_model, to_model, from_attempt, to_attempt, reason}` | Q16 |
| `cost_breakdown` | `{generation_usd, verification_usd, retry_usd, total_usd, currency, provenance}` | Q14–Q17 |
| `outcome` | `{status ∈ pending/accepted/rejected/merged/deployed/rolled_back/reverted/partial, source, recorded_at, reference, human_intervention}` | Q18 |
| `attestation` | reserved `{kind, signer, signature, signed_at}`; only presence is reported | Q19 |

Everything else is derived from existing fields: `execution` from
`executor`/`capture`/routing fields; `verification` from `review_checks`,
`osn_verification_contract` and the pipeline booleans; `capture` from
`derive_shard_identity` plus `capture.hook_events_dropped` and the
completeness score; `attempts` from `attempt_number`/`retry_triggered` and
sibling entries with the same `shard_id`; `integrity` from `content_hash`.

## State derivation (priority order)

```
policy deny or approval denied      -> BLOCKED
approval required and pending       -> APPROVAL_REQUIRED
verification failed                 -> VERIFICATION_FAILED
verification passed:
    escalation observed             -> VERIFIED_AFTER_ESCALATION
    retries > 0                     -> VERIFIED_AFTER_RETRY
    otherwise                       -> VERIFIED
verification not conclusive:
    approval granted                -> APPROVED   (approval is not verification)
    otherwise                       -> UNVERIFIED
```

Verification status comes from `proof_signals.verification_status_from_receipt`
(`passed | failed | skipped | manual_review | not_run | unknown`). Policy
decision is the resolved one (`deny > ask > allow`, or the explicit
`policy.decision`). Escalation is only "observed" from an explicit block or
from directly persisted sibling attempts with a different model.

## Independence of verification

A check is independent when someone other than the executing agent ran it:

* `verifiers[].independent` when the producer says so;
* OpenShard's own static review checks (`review_checks`): independent;
* the OSN verification contract and the pipeline's own verification run:
  independent (OpenShard executed the command);
* an externally observed agent's check command (hooks): **not** independent,
  `verifier_kind = agent_reported`.

## Cost per verified successful task

```
total_usd            = generation + verification + retry        (this attempt)
attempts_total_usd   = total_usd + Σ prior attempts' cost       (same shard)
cost_per_verified_success_usd = attempts_total_usd  if state ∈ VERIFIED states
                                None                otherwise
```

All costs are estimates (`is_estimate` is always true) and carry a
`provenance` (`provider_reported`, `agent_reported`, `openshard_estimated`).

## Capture coverage

`coverage` is `complete` only when OpenShard executed the run
(`origin = openshard_routed`, `capture_depth = full`) and no hook events were
dropped; `partial` for externally observed runs, dropped events, or a session
whose end was not observed; otherwise `unknown`. A fleet-level "coverage
claim" (how many runs OpenShard did not see at all) is future work; the
per-receipt field is the honest building block for it.

## Surfaces

* `ShardReceipt.receipt_state` / `receipt_state_reason` / `receipt_v2_fields`.
* Compact receipt: a `State` row only when the producer wrote any v2 block,
  so v0.4.3 output is byte-identical.
* Full receipt (`--more`, `--full`): a `RECEIPT STATE` block after `TASK`.
* `openshard last --full`: a `RECEIPT ANSWERS` block, one line per question.
* `openshard last --json`: additive `receipt_contract` key.
* `openshard history --json`: additive `state` / `state_reason` per shard.
* MCP `get_receipt`: unchanged default key set.

## Fixtures

`tests/fixtures/receipts/v2/*.json`, each a `runs.jsonl`-shaped record set
with valid content hashes and an `expected` block, labelled as demo data:

1. `01_verified_coding_task` – VERIFIED, merged
2. `02_high_risk_approval_required` – policy ASK, human approved via
   dashboard, VERIFIED; `02b_approval_pending` – APPROVAL_REQUIRED
3. `03_blocked_by_policy` – BLOCKED with denied permission and reason
4. `04_verification_failed` – VERIFICATION_FAILED
5. `05_escalation_then_verified` – two attempts, cheap model failed,
   escalated, VERIFIED_AFTER_ESCALATION, cost per success covers both

The same fixtures seed the OpenShard Cloud dashboard.
