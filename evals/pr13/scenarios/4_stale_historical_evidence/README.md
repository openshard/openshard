# Scenario 4 — Stale historical evidence

**Target repository:** the shared relay seed (`../_shared/relay_seed/`,
commit `d8c94df7…`).

**What this scenario tests.** A *harm* test, not a help test: does
history that is superficially similar to the evaluation task — same
domain vocabulary ("validation", "job", "reject"), same general code
idiom (`RecordError` raised from `Job.__init__`/`_coerce`) — but whose
*specific content* does not transfer, change the outcome for treatment at
all? OpenShard's history should neither help nor mislead here; the
evidence is retrievable (real keyword overlap) but not actionable for
this particular field.

**Burn-in (`burn_in_prompt.txt`):** add validation that `retries` must not
be negative. A genuine, self-contained past fix — confirmed on the base
commit before writing this scenario that no such validation currently
exists.

**Evaluation (`evaluation_prompt.txt`, both arms):** add validation that
`name` must not be empty — a *different* field, same general validation
idiom. Names no files, does not reference the burn-in task.

**Verification:** checks only the evaluation task (empty-name rejection).
Burn-in's own retries-validation feature is not checked by `scenario.
verification` at all — its code is removed by the reset before the arms
run, and its correctness is not gated for this scenario
(`require_verification_failed`/`require_known_failed_approach: false`);
only `require_expected_evidence` (the Shard genuinely exists) is
required.

**Expected effect.** No measurable correctness delta expected. Both arms
should be able to implement empty-name validation on their own — the
worthwhile observation is whether treatment's access to a related-but-
inapplicable prior Shard causes any measurable difference (verified
success rate, tool-call count, wall-clock time) versus control, not
whether it "wins".
