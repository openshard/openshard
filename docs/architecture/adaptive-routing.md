# Adaptive Routing v1

Status: foundation + deterministic baseline, recorded in **shadow mode**. The
model a run executes is still chosen by legacy routing (keyword category ->
provider-aware resolver -> scored selection). The adaptive decision is computed
from the same facts and stored next to it in the Receipt.

## Pipeline

```
Dynamic model catalog (#347)          models/catalog.py
  -> eligibility / policy filtering    routing/adaptive/candidates.py
  -> RoutingContext                    routing/adaptive/context.py
  -> CandidateSet                      routing/adaptive/candidates.py
  -> RoutingPolicy                     routing/adaptive/policy.py
  -> RoutingDecision                   routing/adaptive/decision.py
  -> execute -> verification (#348)
  -> RoutingOutcome                    routing/adaptive/outcome.py
  -> Receipt / routing history          -> evaluation.py -> future policies
```

| Concern | Owner | Rule |
|---|---|---|
| What models exist | catalog | discovery is dynamic |
| What models may be used | `build_candidate_set` | availability, access restriction, `models` policy, harness constraint, per-run block, status, lifecycle, capabilities; one recorded reason per rejection |
| What model should be used | `RoutingPolicy` | selects only from `CandidateSet.eligible` (enforced by `decide_route`) |
| Whether it worked | verification (#348) | status + source + observation mode |
| Evidence for later | `routing_provenance` + `outcome_from_receipt` | recorded, never rewritten |

## Invariants

- A discovered (unpromoted) model is a candidate only when named explicitly (a
  class pin, custom roster entry or explicit model). A newer model is surfaced
  as a `promotion_candidate`, never selected.
- An explicit model is honoured exactly. If it is not eligible the decision
  selects nothing and says why; routing never substitutes another model.
- Equal inputs give equal decisions. `decision_fingerprint` hashes the context,
  catalog fingerprint, policy name/version and the selection.
- `score` and `confidence` are `None` unless a policy computes real values. The
  baseline ranks ordinally and computes neither.
- Harness and model are separate: the context carries the harness, candidates
  apply the harness's provider constraint, and outcomes record both.

## Deterministic baseline (`deterministic_baseline@1`)

The class rules, first match wins (`BASELINE_CLASS_RULES`):

1. `requested_class` from the caller
2. `vision` required -> `vision`
3. visual task -> `vision`
4. security task -> `frontier_reasoning`
5. read-only with a `fast` latency preference -> `fast`
6. boilerplate on high risk -> `balanced_coding`
7. boilerplate -> `cheap_coding`
8. complex -> `balanced_coding` (escalating to `frontier_reasoning`)
9. otherwise -> `balanced_coding`

Within the class, a valid config pin wins; otherwise #347's `filter_for_class`
ranks the eligible candidates. An empty class falls back along
`CLASS_FALLBACKS` (`vision` has none) and the fallback is recorded.

With every model reachable and no policy, the baseline selects exactly what
`select_for_class` selects for each class. It differs from legacy keyword
routing in two places: security tasks request `frontier_reasoning` (legacy
uses the `strong` role), and complex tasks start at `balanced_coding` with a
recovery step to `frontier_reasoning` (legacy uses the `complex` role). Both
differences are visible in shadow provenance (`agrees_with_execution`).

## Recovery

`RecoveryPlan` is fixed with the decision: the escalation ladder above the
resolved class (`cheap_coding -> balanced_coding -> frontier_reasoning`,
`fast -> balanced_coding`, none above `frontier_reasoning` or `vision`), each
step resolved to an eligible model, and `max_attempts` (at most 4).
`next_recovery_action` is pure and escalates only when the last attempt
**failed with observed evidence** (`directly_observed` or stronger). An unknown
or agent-reported outcome stops. Recovery is disabled without runnable checks
or for explicit models. No model or class is tried twice. The run pipeline's
existing escalation loop is not replaced yet.

## Receipt: `routing_provenance`

An additive, optional block on new Receipts. It is not part of the sync
projection. It holds the policy, the selected and executed model, whether they
agree, the requested and resolved class, fallbacks, reason tokens, up to 8
considered ids, the eligible count, rejection counts (never policy lists), a
rejected pin or explicit model with its reason, promotion candidates, the
recovery plan, the context (no task text) and fingerprints.

`RoutingOutcome` is derived at read time from any Receipt, including v0.4.7
ones without the block, and joins through `fingerprints.decision`.
`verified_success` is only true or false for observed evidence. `attempts` is
unknown after a retry, and cost is unknown when a retry's cost was not
recorded. Human corrections are not in Receipts yet (`None`).

## Evaluation

`evaluation.evaluate_decisions` compares policies over
`REPRESENTATIVE_SCENARIOS` and flags contract violations: an ineligible
selection, an unpromoted model without an explicit choice, a deprecated model,
or a substituted explicit model. `summarize_outcomes` computes verified success
rate, cost per verified success (failed spend included; withheld when any
verified outcome lacks cost), latency, attempts, human corrections and shadow
agreement over known values only, and reports coverage. Routing regret needs
counterfactual outcomes and is deferred; `expectation_mismatches` is the
offline stand-in.

## Extension points

A new policy implements `RoutingPolicy` (`name`, `version`, `decide`). It
receives the same context and candidate set and cannot widen eligibility, so
historical-performance, learned, agent-as-a-router or bounded decision-engine
policies plug in without execution changes. The next steps are:

1. Evaluate a policy against recorded shadow decisions and outcomes.
2. Switch the run path to `record_mode: "applied"` for that policy behind a flag.
3. Drive the existing retry loop from `next_recovery_action`.
