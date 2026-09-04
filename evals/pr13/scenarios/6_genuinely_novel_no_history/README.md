# Scenario 6 — Genuinely novel task, no useful history

**Target repository:** the shared relay seed (`../_shared/relay_seed/`,
commit `d8c94df7…`).

**What this scenario tests.** The true empty-useful-history case: history
exists (a real Shard is captured), but it is genuinely unrelated to the
evaluation task in every way `relevant_context` scores on. Distinct from
Scenario 5 (a real but deliberately weak match) — this scenario verifies
a literal zero-match result.

**Burn-in (`burn_in_prompt.txt`):** a one-line, docs-only change (`Add a
one-line License note near the top of README.md`) — touches no code
file, mentions neither "relay" nor any code vocabulary.

**Evaluation (`evaluation_prompt.txt`, both arms):** add a `watch`
command that polls the queue file for changes and reprints the job list —
a genuinely novel capability (nothing else in this codebase polls or
watches anything). Names no files, has no connection to licensing or
documentation.

**Confirmed, not assumed:** before finalizing this scenario, a synthetic
history entry for the burn-in task was scored against the evaluation
prompt directly through `history_query.relevant_context` —
`matches == 0`, and `context_text` reads exactly "No relevant prior
OpenShard history found for this task." (see `tests/
test_pr13_scenarios_2to7.py`).

**Why `require_expected_evidence` is false (and what the results show).**
The benchmark's generic expected-evidence check has three sub-checks:
at least one shard exists; a shard matches the declared task text and
files; and that shard is *retrievable* via `relevant_context(evaluation
prompt)`. The first two pass for this scenario and confirm the burn-in
was genuinely captured (task mentions "license", files are `README.md`
only). The third is a positive-retrievability assertion that a zero-match
scenario cannot satisfy by definition, and it folds into the overall
`present` flag — so `expected_evidence_present` will read **false** in
`benchmark.json`/`run.json` for this scenario, by design, exactly as it
does for Scenario 5. A first live attempt with this flag set to `true`
aborted with `burn_in_evidence_missing` for precisely this reason; the
flag was corrected rather than the history being made artificially
retrievable. The scenario's own tests assert the zero-match outcome
directly against real captured history.

**Verification:** black-box, via a real subprocess. `relay watch` is
started, the queue file is mutated with a separate `relay add` call, and
the watcher's own stdout is polled (bounded waits, the process always
killed in a `finally`) for both the initial and the updated job list —
no assumption about internal API shape, since the prompt names none.
Confirmed directly: the base commit fails in under 5 seconds (no watch
command, no hang); a correct polling implementation passes in under 2.

**Expected effect.** A harm/null-condition test: treatment's `.openshard/`
history is non-empty, but nothing in it is relevant to `watch`. Treatment
should behave identically to control — there is no evidence available to
help or mislead it either way.
