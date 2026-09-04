# PR13 — final benchmark summary

**Status:** closed. Six scenarios completed and valid (1–6). Scenario 7 is
**inconclusive / not completed** and remains an unmeasured limitation.

**Setup, identical for every completed scenario:** model `sonnet` (Claude
Code reported `claude-sonnet-5` in every arm of every run); 3 A/B repeats
per scenario from one burn-in; both arms with the same tool surface (one
`openshard` MCP server: control = placebo with empty history, treatment =
production server over the preserved burn-in history); zero validity
errors recorded in any completed run. Source of every number below: the
`comparison.json` of each run under `evals/pr13/results/` (git-ignored,
kept on the machine that ran them).

## Results (3 runs per arm)

| # | Scenario | Control verified | Treatment verified | Repeated known-bad approach (C / T) | OpenShard tool called (C / T) |
|---|---|---|---|---|---|
| 1 | Previously failed approach | 3/3 | 3/3 | 0 / 0 | 3 / 2 |
| 2 | Multi-attempt chronology | 3/3 | 3/3 | 0 / 0 | 3 / 3 |
| 3 | Prior engineering constraint | 3/3 | 3/3 | 0 / 0 | 1 / 3 |
| 4 | Stale historical evidence | 3/3 | 3/3 | 0 / 0 | 3 / 1 |
| 5 | Irrelevant nearby history | 3/3 | 3/3 | 0 / 0 | 1 / 2 |
| 6 | Genuinely novel, no useful history | 0/3 | 1/3 | (n/a)¹ | 3 / 2 |
| 7 | Cross-agent handoff (OpenCode → Claude) | — | — | — | — |

¹ Scenario 6 has no known-bad approach; its `known_failed_approach` block
is the schema-required placeholder (hidden tests failed and `README.md`
touched). The `1 / 1` the raw `comparison.json` shows for it is that
placeholder matching, not a repeated bad approach, and is not gated.

"OpenShard tool called" counts runs in which the agent invoked an
`mcp__openshard__*` tool. In control that tool is the placebo and always
answers with empty history; in treatment it is the production server. For
Scenario 6 the treatment history is real but, by design, unretrievable
for the task (zero matches), so a treatment call there also returned
nothing useful.

Burn-in preconditions held in every completed scenario: the captured
prior attempt failed verification through the intended known-bad
approach where one was defined (1–5), and the preserved history was
retrievable for the evaluation prompt in 1–5 and correctly *not*
retrievable in 6.

## Interpretation

- **Small n.** Three paired runs per scenario. Nothing here is
  statistically significant, and none of it is a causal claim.
- **No convincing correctness uplift.** In Scenarios 1–5 both arms
  verified 3/3; the treatment's access to genuine OpenShard evidence did
  not change a single verified outcome. The strong-model ceiling seen in
  the Scenario 1 pilot held across every task designed to be tractable.
- **No clear systematic harm.** Stale (4) and irrelevant (5) history did
  not cost the treatment arm a single verified run, and Scenario 6's
  treatment arm did marginally better (1/3 vs 0/3), not worse.
- **Retrieval worked.** The treatment agent called the OpenShard MCP tool
  in 13 of 18 completed treatment runs, and in Scenarios 1–5 the
  preserved burn-in Shard was confirmed retrievable for the evaluation
  prompt before any arm ran. The integration functions; the question this
  benchmark could not answer positively is whether that evidence changes
  outcomes on tasks this model already solves.
- **Scenario 6 showed model variance and a harder task.** The `watch`
  command was the only task the model did not reliably solve (0/3 and
  1/3). With no usable history in either arm by design, the 1/3 vs 0/3
  difference is model variance on a harder task, not an OpenShard
  effect.
- **Scenario 7 remains an unmeasured limitation.** No valid cross-agent
  benchmark result exists. See below.

## Scenario 7: inconclusive / not completed

Three live attempts were made; none produced a valid burn-in precondition,
and no A/B arm ever ran. Recorded factually from each run's own
artifacts:

1. **v1** (`pilot-scenario7`) — OpenCode received an invalid model id
   (the benchmark's Claude Code alias `sonnet` was passed to
   `opencode run --model`, which requires `provider/model`). OpenCode
   replied `Model not found: sonnet/.`, exited 1, made no attempt.
2. **v2** (`pilot-scenario7-v2`) — with a valid `provider/model`, OpenCode
   reached OpenRouter but the model call failed: `402 Insufficient
   credits`. OpenCode exited 0 with zero tool calls and zero file
   changes; no attempt was made.
3. **v3** (`pilot-scenario7-v3`) — OpenCode ran successfully, but the
   burn-in **passed** verification: the agent implemented `tags`
   correctly rather than through the known-bad hand-edit of the generated
   schema, so the required failed-prior-attempt precondition was not met
   (`burn_in_did_not_fail`).

Therefore no valid cross-agent benchmark result was recorded. Whether
OpenShard evidence captured by one agent (OpenCode) transfers to another
(Claude Code) is **unmeasured** by PR13. Scenario 7 is not being tuned or
rerun as part of PR13; its scaffolding (the `opencode_hooks` burn-in path,
its fake, and its tests) is retained as-is for a future attempt, and its
own `metadata.json`/`README.md` carry this status.

## What PR13 did establish

- A reproducible, isolated A/B harness against the real Claude Code CLI:
  pinned commits, independent clones, identical tool surfaces (placebo vs
  production MCP servers, probed and fingerprint-matched before every
  arm), scrubbed environments, external verification, and loud aborts
  instead of substitution.
- Genuine capture of prior attempts through OpenShard's production paths
  (Claude Code hooks; `openshard wrap --shard` for real multi-attempt
  Shards), never fabricated history.
- Two documented product-side findings, not fixed by PR13: external-agent
  capture does not persist the verification fields PR11
  `RecoveryObservation` needs (Scenario 2), and a benchmark's `--model`
  must never be handed to a different agent's CLI (Scenario 7).
