# Routing architecture

How OpenShard decides which model runs a step, why permanent model tiers are no
longer routing authority, how new models enter and get promoted, and what the
deterministic policy does today.

Related: `docs/architecture/adaptive-routing.md` (the V1 shadow pipeline and
its Receipt block), `docs/osn-run.md` (where routing is applied).

## Pipeline

```
Live model catalog                      models/catalog.py (registry + provider discovery)
  -> availability / policy / capability  routing/adaptive/candidates.py
  -> routing context for THIS STEP       routing/adaptive/context.py
  -> routing policy                      routing/adaptive/policy.py (V1), policy_v2.py (V2)
  -> selected model                      routing/adaptive/decision.py
  -> execution                           osn/loop.py, run/pipeline.py
  -> independent verification            history/verification.py
  -> Receipt                             routing_provenance / adaptive_routing blocks
  -> observed outcome history            routing/adaptive/outcome.py, stats routing
  -> future routing decisions            routing/adaptive/history_evidence.py
```

## Four kinds of data, kept apart

Routing mixes four categories of information. They live in different places
and no rule may treat one as another.

| Category | What it is | Where it lives | Authority |
|---|---|---|---|
| **Model facts** | provider, current id, family, release date, expiry, context size, modalities, tool / structured-output / reasoning support, current price, listed or not | `CatalogEntry` (provider discovery, cached OpenRouter list; curated approximations as fallback) | hard requirements and factual ranking components |
| **Routing requirements** | what *this step* needs: requirement class, required capabilities, minimum context, read/write, risk, verification, cost sensitivity, harness, attempt, models tried, last observed verification, accumulated spend | `RoutingContext` + `RequirementClass` | what a candidate must satisfy |
| **Observed performance** | verified outcomes per model / class / harness, retries, escalations, cost, latency when known, evidence coverage | Receipts, derived at read time (`RoutingOutcome`) | ranking, only when the sample is meaningful; recorded as `not_used` otherwise |
| **User / org policy** | allowed / blocked models and providers, pins, custom roster, cost cap, budget, dogfood candidates, access restrictions, capability grants | `ModelPolicyConfig`, `agent_budgets`, Platform capabilities | eligibility and explicit overrides |

Curated **tier**, **roles**, **latency_class**, **experimental** and **cost_class**
(`registry.LEGACY_ADVISORY_FIELDS`) are none of these. They are hand-assigned
labels that predate discovery. They are kept for display, old Receipts and
existing configs, and as an ordering hint of last resort. They are not routing
authority: no requirement class may require a tier or role string, and the
requirement ranking reads them only after facts, promotion state and evidence.

## Why static tiers stopped being authoritative

Model releases now arrive faster than OpenShard releases. A registry that says
`z-ai/glm-5.1` *is* "mid" and `deepseek/deepseek-v4-flash` *is* "cheap"
freezes a judgement made in April into every run in September, while the
provider list already shows GLM 5.2 / 5.3, DeepSeek V4.1 Flash, Claude Sonnet 5
and Opus 5.5, Kimi K3 and others. Worse, two curated ids
(`minimax/m2.7`, `anthropic/claude-opus-4.8-fast`) were never listed by the
provider at all, and one of them was the default for the `complex` role. A
tier string cannot notice any of that.

So the design is now:

* facts come from the provider and refresh without a release;
* a model's *route into routing* is a **promotion state** derived from facts and
  an explicit curation or configuration act, not a quality label;
* a requirement class describes the **work of the step** and is resolved against
  the current eligible pool;
* the stable (capability-off) behaviour is left untouched so nothing changes for
  public users until the Platform capability applies.

## Promotion states (`models/promotion.py`)

| State | How a model gets there | May be selected by |
|---|---|---|
| `discovered` | the provider lists it | nobody (explicit naming only) |
| `eligible_for_shadow` | listed, current, priced, known context | shadow reporting only (`shadow_candidates`) |
| `dogfood_candidate` | named in `models.dogfood_candidates` for a requirement class (config, no release), or curated `experimental` | Routing V2 under the `adaptive_routing` capability, for that class |
| `validated` | curated `active_specialist` | requirement classes it fits |
| `stable` | curated `active_default` | the public default pool |
| `retired` | curated `deprecated`, provider expiry, or absent from a **fresh** provider snapshot | nobody; still readable in old Receipts |
| `restricted` | access-restricted id | nobody |

Two honesty rules: a stale cache cannot retire a model (absence from an old
list proves nothing), and a `retired` state wins over any dogfood naming.

### How a new model enters and is promoted

The Openshard repository currently dogfoods `z-ai/glm-5.3`,
`z-ai/glm-5.3-flash` and `deepseek/deepseek-v4.1-flash` from its repository
`config.yml`. They remain watchlist/shadow models for everyone else. This is
deliberate: newer does not mean better, so public promotion still waits for
Openshard's own verified outcomes and eval evidence.

1. **Discovery.** `openshard models sync-openrouter` (or any catalog command
   with a stale cache) refreshes the provider list. The model is now
   recognisable, displayable and explicitly selectable (`--model`, a pin, a
   roster entry). No Core release.
2. **Shadow.** If its facts qualify, every requirement-class selection lists
   it under `shadow_candidates` ("would qualify if promoted"). Receipts under
   Routing V2 record the same. No Core release.
3. **Dogfood.** The organisation adds it to `.openshard/config.yml`:

   ```yaml
   models:
     dogfood_candidates:
       routine_coding: [z-ai/glm-5.3]
       deep_reasoning: [anthropic/claude-opus-5.5]
   ```

   With the `adaptive_routing` capability on, Routing V2 ranks that candidate
   first for that class (it still has to meet the class's hard requirements and
   is still excluded after it fails a step). Verification is still run by
   OpenShard, the escalation ladder still exists, and the Receipt records
   `promotion_state: dogfood_candidate`. With the capability off nothing
   changes. No Core release.
4. **Validated / stable.** After enough independently verified outcomes
   (`openshard stats routing` shows coverage), curation sets the lifecycle in
   the registry, and the public default moves with it. This is the one step
   that is a release, on purpose: public defaults are reviewed.
5. **Retired.** Curation sets `deprecated`, or the provider stops listing the
   id and the next fresh snapshot retires it on its own.

## Requirement classes (`routing/requirements.py`)

A requirement class is the vocabulary a step uses to say what it needs.

| Class | Hard requirements | Preference |
|---|---|---|
| `fast_control` | listed, current | cheap price band, tools, fast |
| `routine_coding` | tools | mid price band, structured outputs |
| `deep_reasoning` | tools, reasoning | none |
| `vision` | image input | tools |
| `long_context` | tools, >= 500k context | none |
| `verifier` | tools | mid price band, reasoning |

Legacy classes map onto them (`cheap_coding` is `routine_coding` under high
cost sensitivity; `frontier_reasoning` is `deep_reasoning`; `fast` is
`fast_control`), so existing `models.routing_classes` pins keep working and
pins may now use either name. After an observed failure a step escalates
along `ESCALATION_TARGET` (`fast_control -> routine_coding -> deep_reasoning`;
`verifier` and `long_context` -> `deep_reasoning`; `vision` and
`deep_reasoning` retry their own class with the tried models excluded).

### Ranking: ordered, decomposable, recorded

`rank_for_requirement` applies, in order, and records every part per candidate:

1. hard requirements: promotion state selectable, provider status current,
   required tags, minimum context, not already tried (each rejection keeps its
   reason);
2. `promotion`: a dogfood candidate named for this class first (dogfood on),
   then stable / validated, then other dogfood candidates;
3. `history`: observed evidence, only when the caller passed evidence it judged
   meaningful; ranked by cost per verified success; a model with observed
   failures only ranks last; otherwise `not_used`;
4. `requirement_fit`: preferred tags missing;
5. `supersession`: within one family from one vendor, the newest release first
   (the provider's own point-release order, not a claim about quality);
6. `price_band`: inside the class's preferred output-price band, tightened by
   high cost sensitivity or removed by low;
7. `curated_hint`: the legacy role hints, advisory and last;
8. `price`: current output price, cheapest first;
9. model id.

There is no aggregate "quality score". The first version uses ordered rules
because every rule can be named in a Receipt; a numeric combination would
have to be justified with data OpenShard does not have yet. Price is neither
first nor alone: requirements, evidence, promotion and supersession all come
before it, and the target metric is **cost per independently verified
successful task**, not token price.

## Routing V2: trajectory-aware, deterministic (`routing/adaptive/policy_v2.py`)

Behind the `adaptive_routing` capability, `openshard osn run` no longer picks
one tier for the whole run. `TrajectoryPolicyV2` implements the existing
`RoutingPolicy` interface and decides at each **step boundary** OpenShard can
honestly observe (`routing/adaptive/step_types.py`):

| Step | When | What the context carries |
|---|---|---|
| `execute` | attempt 1 | task category, capability needs, harness, spend cap, dogfood allowed |
| `repair` | attempt n>1, after OpenShard itself observed the previous attempt fail verification and the loop decided to retry | everything above plus attempt number, models tried, last verification status and source, accumulated spend |

Inspection and verification are OpenShard's own work, not model calls, and are
not routed. Planning and review stages exist in `openshard run`, which still
runs legacy routing and records only a shadow decision, so those step names
are not claimed yet.

The policy, in order:

1. an explicit model is honoured exactly or nothing is selected;
2. the budget: a `repair` step with spend at or over the cap, or with unknown
   spend under a cap, selects nothing (`cost_budget_exhausted`,
   `spend_unknown_under_cap`);
3. a `repair` step needs an **observed** failure (`directly_observed` or
   stronger). Unknown, `not_run`, or agent-reported outcomes are not success
   and not a reason to escalate: nothing is selected
   (`failure_not_directly_observed`);
4. the requirement class from the context (`V2_CLASS_RULES`), escalated once
   per failed attempt along `ESCALATION_TARGET`, with every tried model
   excluded (`already_tried`);
5. a valid pin wins inside the class;
6. otherwise the requirement ranking above, with dogfood candidates competing
   because the capability is on, and observed history only when the evidence
   gate opens (next section);
7. an empty class falls back to its escalation target, recorded;
8. shadow candidates are reported, never selected.

The decision fixes a recovery plan (each escalation target resolved now, then
one different model in the top class) that becomes the escalation ladder. At
each observed failure the supervisor first runs the recovery envelope that
already existed (`next_recovery_action`: attempt cap, spend cap, observed
failure, no model twice); only if it says *escalate* is the `repair` step
re-decided over the run's own candidate pool (`OsnRouting.reroute`). The
re-route may pick a different model than the fixed plan or say nothing is
eligible, which stops the run. It can never widen what the envelope allowed.
The supervisor record shows the re-route's policy, class, model, reasons and
whether it changed the plan.

### Observed history: gated, never invented

`routing/adaptive/history_evidence.py` derives per-model evidence from
Receipts (`RoutingOutcome`), counting only outcomes whose verification
OpenShard or an independent system observed. Evidence for a model is
*meaningful* only with at least 5 verified outcomes for the same harness, and
the policy uses history only when at least 2 eligible candidates have
meaningful evidence. Cost per verified success is reported only when every
verified outcome in the sample has a known cost. Otherwise the decision
records `history_evidence.used: false` with the reason
(`no_history`, `insufficient_observed_data`,
`too_few_candidates_with_evidence`). Today this is almost always the case,
and the Receipt says so.

### What the Receipt answers

`adaptive_routing` (the entry block) and `routing_provenance` together say:
what step and requirement class were routed; how many models were eligible
and how many were rejected per reason; the selected model, its promotion
state, and the ranking components of the models it was compared with; which
policy and version decided; whether the decision was shadow or applied and
what executed; which discovered models would have qualified
(`shadow_candidates`); whether history was used and why not; the ladder and
whether recovery was enabled. `supervisor_routing` says whether the route
changed later, on what evidence, and whether it was acted on. The
`verification` block says pass / fail / not run / unknown, and
`estimated_cost` what it cost. `capability_snapshot` says which capabilities
governed the run and that they were read at its start. All of it is bounded
(at most 8 considered ids, 5 ranking rows, 3 shadow candidates) and carries
no task text.

### Run-level capability snapshot

`LazyCapabilities(refresh=True)` (`sync/capabilities.py`) reads the
organisation's enabled capabilities once, at the start of a new OSN run,
bypassing the positive cache so a dashboard toggle applies to the next run
immediately. The answer is frozen for the run: budgets, routing and the
supervisor all read the same snapshot, and a toggle flipped mid-run changes
nothing until the next run. Nothing is read until a feature asks (an explicit
`--model` run still makes no request), the negative cache still spares a
Platform that just failed, and offline or unconfirmed still means every
capability is off.

### Toward a learned router

A learned router is a third `RoutingPolicy` over the same structured inputs:
`RoutingContext` (step, class, attempt, tried models, observed failure,
spend) and per-candidate facts and evidence. It replaces the ordering in
`rank_for_requirement`; everything around it (eligibility, explicit choices,
budget and evidence rules, the "only from the eligible set" contract that
`decide_route` enforces, the Receipt) stays. It is not trained yet because
the data is not there: history has hundreds of runs but few with
independently observed verification, and none yet with V2 decisions recorded
beside outcomes. The data needed first, all of which V2 now records: per
step, the context, the eligible set and rejections, the ranking components,
the chosen model and promotion state, the observed verification result and
source, the cost, and whether a re-route or escalation followed. Once
`openshard stats routing` shows meaningful coverage per (class, model,
harness), the policy can be evaluated offline against those records before
it is allowed to choose.

## What remains for compatibility

* `MODEL_CHEAP` / `MODEL_MAIN` / `MODEL_STRONG` / `MODEL_ESCALATE` /
  `MODEL_VISUAL` / `MODEL_COMPLEX` and the legacy routing classes: the stable
  behaviour when no capability applies. Unchanged, except that `complex` no
  longer resolves to the retired `minimax/m2.7` id (it selects on the
  `long_context` fact and lands on `minimax/minimax-m3`).
* `ModelEntry.tier`, `roles`, `latency_class`, `experimental`, `cost_class`:
  displayed (labelled legacy in `models show`), used by old Receipts and
  advisory tooling, and as the last ordering hint.
* Old Receipts naming `z-ai/glm-5.1`, `deepseek/deepseek-v4-flash` or
  `minimax/m2.7` render and derive outcomes exactly as before; nothing is
  rewritten.
* `models.routing_classes` pins keep their legacy names.

## What is no longer authoritative

* A tier or role string as a routing filter for new work.
* A hardcoded id as a default: `_FALLBACKS` remain only for a pathological
  registry failure and are never consulted when the catalog resolves.
* "Newest is best": a newer release is surfaced (shadow / promotion candidate)
  and, within one family, supersedes its older sibling among equally promoted
  models; it never jumps a promotion state on its own.
* Curated `cost_class` as the price signal when the provider reports a price.

## Stale defaults, concretely

* `minimax/m2.7` (never listed): curated `deprecated`; `complex` selects the
  curated long-context model.
* `anthropic/claude-opus-4.8-fast` (not listed): moved to `watchlist`; it
  cannot be verified against a provider and is not a default.
* `z-ai/glm-5.1`, `deepseek/deepseek-v4-flash`, `anthropic/claude-opus-4.7`,
  `moonshotai/kimi-k2.5`: still listed and current, so they remain the stable
  public defaults. Their successors (GLM 5.2 / 5.3, DeepSeek V4.1 Flash, Opus
  5 / 5.5, Kimi K2.6 / K3) are in the catalog as `eligible_for_shadow`, appear
  as shadow / promotion candidates, and can be named as dogfood candidates
  today. Promoting one to the public default is a curation act after observed
  evidence, not a version-number comparison.
* Guard tests (`tests/test_routing_dynamic_candidates.py`) run every class
  against a checked-in real provider snapshot and fail if a default is
  `retired` or `unlisted`, or if any stable / validated id is missing from the
  provider list.
