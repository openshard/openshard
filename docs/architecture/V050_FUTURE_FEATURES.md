# v0.5 future features: interfaces and TODOs (public runtime side)

Each item names the contract that already exists in the public package, the
receipt fields it must populate, what is done, and the TODO. The hosted
counterparts live in the private `openshard-cloud` repository
(`docs/ROADMAP.md` there). Nothing here is built beyond what "Done" says.

## A. Team RBAC

* Contract: none in the public package by design. Roles are a hosted
  concept; the runtime only carries principals on the receipt
  (`actors.owner`, `requested_by`, `executed_by`, `approved_by`).
* Done: `Principal` with `kind ∈ {user, agent, service, team, policy}` and
  the `actors` block; the dashboard shows them.
* TODO: `openshard config` keys `owner:` / `requested_by:` (or env
  `OPENSHARD_OWNER`, `OPENSHARD_REQUESTED_BY`) so producers stamp `actors` at
  write time; the pipeline (`_log_run`) and `build_hook_entry` write the block
  when configured. Never infer an owner from git config without consent.

## B. Agent permissions

* Contract: `receipt_contract.permissions` (`requested / used / denied`).
* Done: the block is read, rendered and synced; `command_policy` (allowed /
  blocked paths, blocked commands) is folded in for old records.
* TODO: producers write `permissions.used` from the tool trace (native
  runs: `run_tool` calls; hooks: file/command tool kinds) and
  `permissions.requested` from the plan / form factor; a permission string
  grammar (`read:<glob>`, `write:<glob>`, `shell:<summary>`,
  `network:<host>`), path-free and secret-free.

## C. ALLOW / DENY / ASK policy evaluation

* Contract: `openshard.contracts.policy.PolicyEvaluator` → `PolicyVerdict`
  (decision + reason + `policy_id/version/name`), reusing
  `policy.decision.PolicyDecision` and `resolve_policy_decisions`.
* Done: the verdict maps onto `policy` and `policy_decisions`; the receipt
  state treats `deny` as `BLOCKED`, `ask` as approval required.
* TODO: `GatePolicy` wrapping `execution.gates.GateEvaluator` so today's
  gates emit a verdict with policy identity (`builtin:gates`, version =
  package version); a file policy (`.openshard/policy.yml`) evaluated
  offline; `policy.evaluated_at` stamped; hooks record `deny` decisions
  when the capture service refuses a path.

## D. Human approvals

* Contract: `openshard.contracts.approvals.ApprovalProvider`.
* Done: `ApprovalOutcome.to_receipt_block()` → `approval` block with
  approver, mechanism and timestamp; states `APPROVAL_REQUIRED` /
  `APPROVED` / `BLOCKED`.
* TODO: `CliPromptApprovalProvider` around the pipeline's `click.confirm`
  (`mechanism = cli_prompt`, approver = configured local principal);
  `openshard approval wait <request_id>` polling a hosted provider; the
  pipeline persisting `approval` instead of only `approval_receipt`.

## E. Managed Compute provider abstraction

* Contract: `openshard.contracts.compute.ManagedComputeProvider`;
  `UnavailableComputeProvider` is the explicit default.
* Done: the boundary and the `ComputeRunStatus.receipt_shard_id` link.
* TODO: `openshard run --managed` submitting a `ComputeRunSpec` to a hosted
  provider and syncing back the resulting receipt with
  `capture.coverage = complete`; no local provider will be implemented.

## F. Model escalation after failed verification

* Contract: `receipt_contract.attempts.escalation`;
  `VERIFIED_AFTER_ESCALATION`.
* Done: derived from sibling attempts with a different model, or from an
  explicit `escalation` block; cost per verified success sums attempts.
* TODO: `openshard run --retry-of <shard_id> --escalate` choosing the next
  model in `routing.model_resolver.ESCALATION_CHAIN` and writing the
  `escalation` block at run time; an escalation budget in policy (C).

## G. Cost-per-verified-task analytics

* Contract: `receipt_contract.cost` (`generation / verification / retry /
  total / attempts_total / cost_per_verified_success`).
* Done: per-receipt derivation; `verifiers[].cost_usd` and
  `cost_breakdown` blocks; hosted fleet average.
* TODO: `openshard stats cost` grouping by agent / model / policy with
  "cost of unverified work"; producers stamping `cost_breakdown` when the
  verification step has a known cost (LLM judges, CI minutes).

## H. Receipt coverage / missing telemetry detection

* Contract: `receipt_contract.capture` (`coverage`, `hook_events_dropped`,
  `session_end_observed`, `completeness_percent`).
* Done: per-receipt coverage, never `complete` unless OpenShard executed
  the run with nothing dropped.
* TODO: `openshard stats coverage` comparing receipts against independent
  signals in the repository (commits whose trailers name an agent, CI runs
  on agent branches) to report "observed N of M"; the capture service
  recording a `capture.gap` event when it restarts with a non-empty queue.
