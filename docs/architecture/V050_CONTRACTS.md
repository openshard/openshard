# v0.5 Architecture Contracts

Package: `openshard/contracts/`. Six boundaries, each a request/result pair
plus a `Protocol`. No implementation beyond trivial ones; no I/O; the local
runtime needs none of them configured.

| Boundary | Protocol | Result → receipt block | Shipped implementations |
| --- | --- | --- | --- |
| Verification | `Verifier.identity() / verify(VerificationRequest)` | `VerificationOutcome.to_receipt_block()` → `verifiers[]` | `NullVerifier` (`not_run`, "no verifier configured") |
| Policy | `PolicyEvaluator.evaluate(PolicyContext)` | `PolicyVerdict.to_receipt_block()` → `policy`; `to_policy_decisions()` → `policy_decisions[]` | `AllowAllPolicy` (reason says no policy is configured) |
| Approvals | `ApprovalProvider.request(ApprovalRequest) / check(id)` | `ApprovalOutcome.to_receipt_block()` → `approval` (+ `actors.approved_by`) | `RecordingApprovalProvider` (scripted, in-memory) |
| Sync | `SyncTransport.push(SyncEnvelope)` | n/a (envelope carries `receipt_contract` + `receipt`) | `RecordingSyncTransport`; HTTPS transport in `openshard/sync/` |
| Managed Compute | `ManagedComputeProvider.submit / status / cancel` | `ComputeRunStatus.receipt_shard_id` links to a receipt | `UnavailableComputeProvider` (raises `ComputeUnavailableError`) |
| Outcomes | `OutcomeReporter.report(OutcomeReport)` | `OutcomeReport.to_receipt_block()` → `outcome` | `RecordingOutcomeReporter`; local recorder `openshard/history/outcomes.py` |

## Decisions

1. **Reuse `PolicyDecision`.** The verdict wraps the existing decision list
   and resolves it with `resolve_policy_decisions` (deny > ask > allow). What
   is new is policy identity (`policy_id`, `policy_version`, `name`) so a
   receipt can say which policy governed the run.
2. **Fail closed on vocabulary.** An unknown verdict becomes `deny`; an
   unknown approval status stays `pending`; an unknown verification status
   becomes `unknown`; an unknown outcome status raises at construction.
3. **Approver is its own principal.** `ApprovalOutcome.approver` is never
   defaulted from the requester or executor. An auto-policy approval has
   `mechanism = auto_policy` and an approver of kind `policy`.
4. **Outcomes live beside the run, not in it.** `openshard outcome record
   <shard> merged --reference "PR #341"` appends to
   `.openshard/outcomes.jsonl`. The run record is never rewritten, so its
   content hash stays valid. The contract builder takes the latest outcome as
   an overlay (`outcome_record=`).
5. **Sync sends projections, not entries.** The envelope is the Receipt
   Contract v2 dict plus the extended `receipt_to_dict` projection. Both are
   already the privacy boundary used by the MCP server. Raw entries, blocked
   fields, absolute paths and credentials never leave the machine.
6. **Managed Compute is explicit about absence.** There is no silent no-op
   provider; `UnavailableComputeProvider` raises so a caller cannot mistake
   "not configured" for "queued".
7. **No speculative abstraction.** No plugin registry, no dependency
   injection container, no async variants. Each Protocol has one or two
   methods. When a second real implementation exists, that is the moment to
   generalise.

## What wraps what (future)

* `openshard.verification.executor.run_verification_plan` and the OSN
  verification loop → a `LocalRunnerVerifier` (`verifier_kind =
  openshard_runner`, `independent = True`).
* `openshard.execution.gates.GateEvaluator` → a `GatePolicy` evaluator that
  emits `ask` decisions with the gate's reason.
* The pipeline's `click.confirm` → a `CliPromptApprovalProvider`
  (`mechanism = cli_prompt`, approver = the local user principal when
  known).
* OpenShard Cloud → `DashboardApprovalProvider`, `HostedSyncTransport`
  (exists as `openshard.sync.HttpsSyncTransport`), `CloudComputeProvider`,
  `GitHubOutcomeReporter`.
