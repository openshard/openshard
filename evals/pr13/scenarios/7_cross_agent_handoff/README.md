# Scenario 7 — Cross-agent handoff

> **Status: inconclusive / not completed under PR13.** Three live attempts
> were made and none produced a valid burn-in precondition, so no A/B arm
> ever ran and no cross-agent benchmark result exists:
>
> 1. **v1** — OpenCode received an invalid model id (the benchmark's Claude
>    Code alias `sonnet`); `Model not found: sonnet/.`, exit 1, no attempt.
> 2. **v2** — OpenCode reached OpenRouter but the model call failed with
>    `402 Insufficient credits`; exit 0, zero tool calls, zero edits.
> 3. **v3** — OpenCode ran successfully, but the burn-in **passed**
>    verification (it implemented `tags` correctly rather than hand-editing
>    the generated schema), so the required failed-prior-attempt
>    precondition was not met (`burn_in_did_not_fail`).
>
> Whether OpenShard evidence captured by one agent transfers to another
> is therefore **unmeasured** by PR13. This scenario is not being tuned or
> rerun as part of PR13; its scaffolding is retained as-is. See
> `evals/pr13/SUMMARY.md`.

**Target repository:** the shared relay seed (`../_shared/relay_seed/`,
commit `d8c94df7…`).

**What this scenario tests.** Every other scenario's history is captured
from a Claude Code session, and every evaluation arm is also Claude Code
— so a treatment win could in principle be partly about *same-agent*
continuity rather than the evidence itself. This scenario breaks that
by using OpenCode as the burn-in agent and keeping Claude Code (the
`--model` the benchmark was given, e.g. Sonnet) as the evaluation agent
in both arms: does OpenShard's evidence still help when it was captured
by a different tool entirely?

## Burn-in mechanism: `opencode_hooks`

`burn_in.capture: "opencode_hooks"` (`burn_in.agent: "opencode"`) installs
OpenShard's *production* OpenCode plugin
(`openshard.adapters.opencode_plugin_install`) into the burn-in workspace
and runs a real, non-interactive `opencode run --dir <ws> --format json
--model <model> "<prompt>"` session (flags confirmed directly against the
installed OpenCode CLI's own `--help`, not assumed). The plugin posts to
the same private capture service the Claude Code path uses; the resulting
Shard's `executor` is `opencode_plugin`, exactly as PR12's production
cross-agent capture already labels it — nothing about OpenCode's evidence
semantics is changed for this benchmark.

Preflight requires the `opencode` CLI to be on PATH and aborts loudly
(`opencode_cli_missing`) if it is not — no fallback, nothing substituted.

## Task and non-leakage

Deliberately reuses Scenario 1's exact mechanism (hand-editing the
generated `relay/_schema.py` instead of `schema/jobs.json` + regenerate)
on a different field (`tags`, not `priority`), so the only real variable
that changed from Scenario 1 is *which agent produced the burn-in
evidence*. The burn-in prompt explicitly restricts the agent's file scope
(`relay/_schema.py`, `relay/queue.py`, test files only) — the same
technique Scenario 1's revision 2 uses to force the known failed approach
deterministically without naming `schema/jobs.json`/the generator (which
would leak the correct approach through the captured Shard's task text).
The evaluation prompt is neutral, exactly like Scenario 1's.

## Verification

The same two-step check as Scenario 1 (`gen_schema.py --check`, then
hidden unit tests for the `tags` field: schema declaration, generated-
module sync, record defaulting/formatting/backward compatibility, CLI
`--tags`). Verified directly against hand-written naive and correct
implementations before finalizing (base commit and the naive approach
both fail `generated_schema_in_sync`; the correct approach passes all 8
hidden tests).

## Live pilot abort and fix

A first live attempt aborted with `burn_in_failed_differently`. The run's
own artifacts showed why: the benchmark's `--model` (the Claude Code alias
`sonnet`) was passed straight to `opencode run --model`, which expects
`provider/model`. OpenCode replied `Model not found: sonnet/.` and exited
1 after ~30s with zero tool calls and zero file changes; its plugin still
captured a task-only Shard (session created, prompt submitted, idle), so
verification ran and failed only because `tags` was never added, while
`gen_schema --check` trivially passed on the untouched tree — neither
known-failed-approach criterion could match, and the abort was
mislabelled. Two scenario-side changes, no change to the shared rules:

* `burn_in.agent_model` (here `openrouter/anthropic/claude-sonnet-5`,
  taken from `opencode models` on this machine — the same Sonnet family
  the Claude arms use) is now required and validated for
  `opencode_hooks`, and is the only thing passed to OpenCode; the Claude
  `--model` is never handed to another agent's CLI.
* The OpenCode burn-in aborts as `burn_in_agent_failed` when the agent
  process exits non-zero, mirroring the Claude path's existing check for a
  session that never started, so a burn-in that never attempted the task
  cannot reach the known-failed-approach check.

`tests/pr13_fake_opencode.py` now reproduces the observed failure exactly
(non-`provider/model` id → the same two error events, idle-not-deleted
capture, exit 1) so the mislabel is regression-tested.

**Second live abort (v2), and the second guard.** A follow-up live
attempt (`pilot-scenario7-v2`) aborted with the *same*
`burn_in_failed_differently`, but the artifacts showed a different cause:
OpenCode accepted `openrouter/anthropic/claude-sonnet-5`, but the model
call itself failed with an OpenRouter `402 Insufficient credits` and
OpenCode **exited 0** — with zero tool calls, zero turns, and the B1 tree
byte-identical to base. The v1 guard only caught a *non-zero* exit, so
this exit-0-no-work case slipped through to verification and was
mislabelled again (`hidden_tests` failed for lack of `tags`;
`gen_schema --check` trivially passed on the untouched tree; neither
criterion matched). This is an **environment condition** (no OpenRouter
credits on this machine), not a prompt, fixture, or detector defect —
the fix makes the abort *accurate*, it does not and cannot make Scenario
7 pass live without credits. The OpenCode burn-in now also aborts as
`burn_in_agent_failed` when the agent exits 0 but made no attempt (0 tool
calls **and** 0 changed files), quoting OpenCode's own error. This is
safe: the known bad approach always edits `relay/_schema.py`, so any
genuine attempt changes at least one file, and a real hand-edit is never
suppressed. The guard lives only in the `opencode_hooks` burn-in branch;
scenarios 1–6 are untouched.

## What is not yet done

This scenario has not been run live — it requires OpenCode installed and
authenticated with a real provider. `tests/test_pr13_scenarios_2to7.py`
covers the orchestration deterministically with a fake `opencode`
executable that calls OpenShard's real `opencode_plugin` translator/fold
path directly (the same technique `tests/pr13_fake_claude.py` uses for
Claude Code hooks), so the benchmark-side logic (plugin install, waiting
for the new entry, verification, the known-failed-approach check) is
exercised without a live OpenCode session.

## Expected effect

Same shape as Scenario 1: control should repeat the known failed
approach; treatment, seeing a prior Shard whose changed files include
`relay/_schema.py` but not `schema/jobs.json`, has a plausible basis to
avoid it — this time regardless of which agent left that evidence.
