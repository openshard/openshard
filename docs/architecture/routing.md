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
