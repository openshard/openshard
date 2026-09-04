# Scenario 5 — Irrelevant nearby history

**Target repository:** the shared relay seed (`../_shared/relay_seed/`,
commit `d8c94df7…`).

**What this scenario tests.** History exists in the same repository but
is topically unrelated to the evaluation task — does OpenShard's
relevance scoring correctly keep it from injecting noise, rather than
surfacing it as if it mattered?

**Burn-in (`burn_in_prompt.txt`):** add a `--version` flag to the CLI.
Deliberately minimal wording, with no trailing "add unit tests / do not
commit" boilerplate — an earlier draft that used the same trailer every
other scenario's prompts share (and named "count"/"queue" in the task
itself) scored 6 against the evaluation prompt purely from that
boilerplate and vocabulary overlap, not genuine relevance. Verified
directly with a synthetic history entry (see `tests/
test_pr13_scenarios_2to7.py`) that the final wording scores exactly 2 —
the minimum possible nonzero score, from the incidental project name
("relay") appearing in both task descriptions and nothing else: no file
overlap, no failure/retry bonus.

**Evaluation (`evaluation_prompt.txt`, both arms):** add `relay rename
OLD_NAME NEW_NAME`. Names no files, does not reference `--version`.

**Verification:** checks only `rename`'s correctness (renames, preserves
other fields and position, rejects a missing source or a colliding
target name). Burn-in's own `--version` feature is not checked here — its
correctness is not gated for this scenario.

**Why this isn't "zero matches".** The project name ("relay") appears
naturally in almost any reasonably-phrased task description for this
codebase, so a literal zero-score, zero-match result turned out not to be
achievable without writing artificially stilted prompts. Genuinely
history-free retrieval is Scenario 6's job instead (a docs-only burn-in
against a fully novel eval task). This scenario instead honestly
exercises the *low end* of the scoring range: a real, present Shard that
scores just barely above zero and carries no actionable signal, which
`relevant_context`'s own rendered explanation ("Why relevant: task
overlap: relay") makes transparent rather than dressing up as a genuine
recommendation.

**Expected effect.** A harm test: `rename`'s correct implementation does
not depend on anything in the `--version` Shard. Both arms should perform
the same; the interesting observable is whether a weak, low-information
match changes treatment's tool use or outcome at all.
