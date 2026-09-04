# Scenario 2 — Multi-attempt chronology

**Target repository:** the shared relay seed (`../_shared/relay_seed/`,
same commit as Scenario 1: `d8c94df7…`).

**What this scenario tests.** A real, same-Shard multi-attempt history: an
earlier attempt that failed the project's own checks, followed by a later
attempt — attempt 2 of the *same* Shard — that corrects it. It verifies
that this chronology is genuinely captured (two attempts, one `shard_id`,
sequential `attempt_number`s) and genuinely retrievable/renderable
downstream (`relevant_context`'s per-attempt status list). It does **not**
test OpenShard's PR11 `RecoveryObservation` feature — see "Documented
limitation" below for exactly why, and why that is a real product gap
rather than something this scenario works around.

## Burn-in mechanism: `claude_wrap_chain`

Claude Code's own hook capture (used by every other scenario) never
records a verification outcome for any attempt — by design, since
OpenShard did not run the check itself and refuses to claim it knows the
result. A hook-captured Shard can therefore never carry a second, *linked*
attempt in the way this scenario needs either (hooks mint a fresh Shard
per session, never an explicit attempt 2 of an existing one). This
scenario instead uses the one real mechanism that both links attempts
explicitly and is non-fabricated: `openshard wrap claude --shard <id>`
(Migration 2's Run/Attempt linkage), run twice —

1. **Stage 1** (`burn_in_stage1_prompt.txt`): a real Claude Code session
   adds `relay purge`, explicitly instructed to write the queue file
   directly rather than through `QueueFile.save()`. This creates a new
   Shard, attempt 1. The benchmark's own hidden tests fail afterwards
   (the direct write violates the project's trailing-default-field
   compatibility rule).
2. **Stage 2** (`burn_in_stage2_prompt.txt`): a second, separate real
   Claude Code session, wrapped with `--shard <the same id>`, fixes
   `purge` to go through `QueueFile.save()`. This becomes attempt 2 of
   the *same* Shard. The hidden tests pass afterwards.

Both stages are genuine, separate `claude -p` sessions; `wrap` writes each
resulting Shard entry synchronously (no capture service, no hooks, no
polling needed). `known_failed_approach`/`require_verification_failed`
apply to **stage 1's own diff and verification**, not the final
(corrected) state; the runner additionally requires the **final** stage's
verification to pass, or the whole benchmark run aborts
(`burn_in_correction_not_confirmed`) — nothing is substituted if a live run
doesn't actually produce a corrected second attempt.

## What this scenario verifies

- A genuine multi-attempt Shard: the same `shard_id` on both entries, with
  `attempt_number` 1 and 2 respectively (`shard_attempt_count == 2`),
  built from two independently real agent sessions.
- OpenShard's `_BONUS_MULTI_ATTEMPT` relevance-scoring bonus (`len(group.
  attempts) > 1`), which needs no verification data at all.
- The per-attempt status list `relevant_context` renders
  (`RelevantMatch.attempts`, shown as "Attempts: 1: ..., 2: ..." in
  `context_text`) — this is the downstream retrieval/rendering of the
  chronology that a treatment agent actually sees.

## Documented limitation: PR11 `RecoveryObservation`

OpenShard's PR11 added `history.query.RecoveryObservation`: given a
Shard's attempts, it derives a verified *fail-then-pass* chronology
(`_find_recovery_pair`, gated on `_attempt_verification_state`) and
`relevant_context` reports it as an explicit "attempt N failed
verification; attempt M later passed" observation. That machinery exists
and works — for Shards whose attempts carry a `verification_passed` or
`osn_verification_contract` value.

**External-agent capture does not currently persist those fields.**
Traced directly against the source before this scenario was built:
Claude Code hook capture (`adapters/claude_hooks.py`) hard-codes
`verification_attempted: False, verification_passed: None` on every
entry; the OpenCode plugin path is the same; `openshard wrap`
(`adapters/wrap_exec.py`) explicitly documents "never invents
verification" and records none. The *only* place in the codebase that
ever writes `verification_passed` is `openshard/run/_pipeline_helpers.py`
— OpenShard's native run pipeline, which PR13 deliberately excludes as
"the coding agent" (its mandate is a real external CLI, not OpenShard's
own model-calling pipeline).

**Therefore PR13 cannot honestly test `RecoveryObservation` against
externally-captured history today.** This is a genuine product gap in
OpenShard's external-capture semantics, not a shortfall in this
benchmark's design, and closing it is a production-OpenShard decision
(deciding what, if anything, an external agent's capture should ever be
allowed to assert about verification) — explicitly out of scope for
PR13, which only benchmarks existing behaviour and must not change
production semantics to make itself pass. This scenario tests the
weaker, real thing instead (the multi-attempt chronology itself) and
says so plainly, rather than calling that a "recovery" test.

## Evaluation task and non-leakage

The evaluation prompt (`relay reset-retries`, both arms) is a **different**
task from burn-in's `relay purge` — it names no files and says nothing
about how the queue file must be written. It shares only domain
vocabulary ("queue", "jobs") with burn-in, which is what would make the
Shard *retrievable* via `relevant_context`'s keyword-overlap scoring; that
overlap is not evidence disclosure by itself.

The Shard's *latest* attempt (stage 2, "Fix relay purge to go through
`QueueFile.save()`") is what `relevant_context` would surface if this
Shard scores relevant to `reset-retries`. This does name the correct
*general* file-writing convention for the codebase — but that is
intentional and is not the same as leaking the eval task's answer: it
describes the fix to a **different, already-completed task**, and
`reset-retries` still requires the agent to write its own logic (which
records to reset, in what order, preserving which fields). Applying the
general principle to a new task is exactly the transfer this scenario is
built to test — unlike Scenario 1, where an identical-task leak would
hand over the literal solution.

## Verification

One shared hidden-test suite (`verification/hidden_tests/
test_purge_and_reset_retries.py`) is used at every stage — burn-in stage 1
(purge, buggy), burn-in stage 2 (purge, fixed), and the evaluation arms
(reset-retries; purge does not exist there, since the code reset removes
every burn-in code change and keeps only `.openshard/`). Each `TestCase`
skips itself (never fails) when its own subcommand isn't registered at
that stage, so `python -m unittest`'s exit code always reflects only the
feature genuinely present. Verified directly against hand-written buggy/
fixed implementations of both commands before this scenario was
finalized (skip-all on the bare base commit; fail on a direct write; pass
once routed through `QueueFile.save()` — for both `purge` and
`reset-retries` independently).

## Expected effect

Primarily a **machinery** scenario: it proves OpenShard can capture and
surface a real multi-attempt chronology from an external agent, and is
explicit that the stronger, verified-outcome `RecoveryObservation`
feature remains architecturally out of reach for any external-agent
history today (see "Documented limitation" above) — closing that gap
would require a production-OpenShard change, which PR13 does not make. A
modest, plausible correctness benefit for treatment is possible (seeing
that a past fix went through `QueueFile.save()`), but `reset-retries`'
correct implementation is also directly discoverable by reading
`queue.py`'s own API, so, unlike Scenario 1, no strong correctness delta
is engineered or expected here.
