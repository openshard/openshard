# `openshard osn run`

A bounded, policy-gated coding task with verification performed by OpenShard.

```
openshard osn run "Implement slugify in slug.py" \
  --verify-cmd "python -m pytest -q tests" \
  --context-file slug.py --context-file tests/test_slug.py \
  [--model M] [--escalate-model M2 ...] [--max-attempts 2] \
  [--task-id task_...] [--promote] [--yes] [--json]
```

## Flow

task -> context -> model proposes whole-file writes -> policy gate -> isolated copy ->
OpenShard runs `--verify-cmd` -> bounded retry / escalation -> receipt.

- The model is called through the existing provider layer (`BaseProvider.execute`), so any
  configured provider works. Without `--model`, the existing keyword routing picks the first model.
- Writes go only to an isolated copy of the repository (local secrets and agent state such as
  `.env` and `.claude/` are not copied). Your repository is untouched unless `--promote` is given
  and the loop's own verification passed.
- Every proposed path passes path-safety checks and the file-mutation policy (deny: secrets,
  `.env*`, `.git/`, `.openshard/`; ask: CI, Docker, `pyproject.toml`, `package.json`). A blocked
  proposal is not retried.
- OpenShard runs the verify command itself and reads its exit code. A leading `python` runs under
  the interpreter OpenShard uses. The verifier runs with your permissions and can execute
  agent-written code: the copy isolates files, not processes.
- A retry happens only after a verification failure, only if the proposed writes and the failure
  output both changed, and never more than 5 attempts. `--escalate-model` models are used only for
  those retries.
- `--promote` copies the verified files into the repository through the same policy gate as
  `apply-last`. It refuses if the files changed after verification, and it does not re-verify in
  the repository.

## Evidence

| Field | Level |
|---|---|
| Proposed writes | agent-declared |
| Policy decisions, file effects | OpenShard-observed; every proposed write is stored as an allow / ask / deny `policy_decisions` entry (with whether an approver granted an ask), and an `approval_receipt` says what approval was needed and whether it was given, so `history`, failure classification and trust scoring treat an OSN policy block as a policy block |
| Verification (exit code) | OpenShard-observed (`directly_observed` / `openshard_executed`) |
| Model cost | recorded only when the provider reported it; otherwise unknown |

A verifier that cannot be started is recorded as `not_run`, never as a pass or a model failure. A
verifier that rewrites the files it is checking does not count as a pass. The Shard entry
(`executor: osn_loop`) stores no task text beyond the usual sanitised task, only the verifier's
executable name, and a routing provenance block, so `openshard stats routing` includes
these runs. The model that ran is chosen by you (`--model`) or by keyword routing, unless the
`adaptive_routing` capability applies (next section).

## Adaptive routing (experimental)

Only when the Platform lists the `adaptive_routing` capability as enabled for the linked
organisation, and only when you did not pass `--model`. Then the adaptive baseline
(`docs/architecture/adaptive-routing.md`) chooses the first model from the catalog, the provider
keys you have, the repository's `models` policy and the task's routing class, and its recovery
plan becomes the escalation ladder when you gave no `--escalate-model`. The loop still climbs that
ladder only after an observed verification failure, and the budget (above) still applies.

What never changes: an explicit `--model` always runs and is never substituted (the capability is
not even looked up); an explicit `--escalate-model` ladder always wins; the ladder is cut to what
`--max-attempts` (and a budget's `max_attempts`) can actually run; a `models:` policy that cannot
be parsed, no eligible candidate, no decision at all, or a selected model the chosen `--provider`
cannot dispatch all fall back to keyword routing and are recorded as `applied: false` with the
reason (`model_policy_invalid`, `no_eligible_candidate`, `decision_unavailable`,
`provider_mismatch`); the capability off, unconfirmed or unreachable leaves what runs as it was.
One cost is new: a run without `--model` in a Platform-linked repository asks the Platform once
whether the capability is on (cached ten minutes; a failed read is remembered for one). Historical
per-model success is not used to route: there is not enough observed data yet, and the record says so.

The Shard entry's `routing_provenance` block gets `record_mode: applied` (compared with the model
that ran first) instead of `shadow`, and an `adaptive_routing` block records whether the decision
was applied and why not otherwise, the selected model and routing class, the `escalation_ladder`,
where it came from (`recovery_plan` or `user`), the `max_attempts` it was cut to, and
`history_evidence: not_used_insufficient_observed_data`. `openshard stats routing` attributes an
applied run to the model the decision chose (its `escalations` column counts how often the ladder
was climbed) and excludes applied runs from the shadow-agreement rate, which only means something
for decisions that did not pick the model.
The block appears in `osn run --json` and the full receipt (`ADAPTIVE ROUTING` section); like the
budget block it is not yet part of the `history --json` / Platform sync projection. With the
capability off, the shadow provenance is unchanged except that a `--model` you passed is now
recorded as an explicit choice rather than as free routing.

## Agent budgets (experimental)

Only when the Platform lists the `agent_budgets` capability as enabled for the linked
organisation (`openshard sync connect`). Without a link, when the Platform cannot be reached or
refuses the key, or when the capability is simply not on, `osn run` behaves exactly as above and a
configured budget is recorded as *not enforced*. Nothing about the link changes: the same
endpoint, organisation and `osk_` key that receipt sync uses are read once per run and the answer
is cached for ten minutes (see `docs/platform-sync.md`).

Configure hard limits in the repository's `.openshard/config.yml`:

```yaml
agent_budgets:
  max_spend_usd: 0.50   # estimated model spend, summed over every call
  max_attempts: 3       # loop attempts (one model proposal + one verify run each)
  max_commands: 3       # verify-command launches by OpenShard
  max_writes: 10        # whole-file writes applied to the isolated copy
```

Every key is optional; an unreadable block (unknown key, zero, negative, a string) is refused
before any work starts. Each limit is checked at the last boundary before that work would
happen, and the first limit that would be exceeded ends the run with status `budget_exhausted`
and a `stop_reason` of `budget_<limit>`. Nothing retries past a budget: the re-ask for a
malformed model reply and every escalation attempt go through the same ledger.

What is, and is not, counted:

| Limit | Counts | Boundary |
|---|---|---|
| `max_spend_usd` | the estimated cost of each model call (the provider's own figure when it reports one, otherwise OpenShard's price table applied to the token counts) | before the next model call |
| `max_attempts` | loop attempts started | before an attempt starts |
| `max_commands` | verify-command launches by OpenShard | before an attempt whose verify run could not happen, and again before the launch |
| `max_writes` | files written into the isolated copy | before an attempt that could apply nothing, and again before each write |

Spend is an estimate and is known only after a call answers, so one call can overshoot the limit;
the Receipt records the estimated total (`spend_is_estimate: true`). A call with no usable cost
(an unknown model, no usage report) makes spend unobservable: with a spend limit configured the run
stops (`budget_spend_unobservable`) rather than treating the cost as zero. Processes the verify command itself spawns are not counted. Promoting verified files with
`--promote` is the separate policy-gated step described above and is not a budgeted write.

The Shard entry carries an `agent_budgets` block: the configured `limits`, the observed `usage`
(`spend_usd` and whether it was known, `model_calls`, `attempts`, `commands`, `writes`), the
`limit_reached` (set whenever usage met a limit, even if the run then ended for another reason)
and the `action` OpenShard took (`none`, `stopped_before_model_call`, `stopped_before_attempt`,
`stopped_before_command`, `stopped_before_write`, `stopped_spend_unobservable`). When a budget was
configured but not enforced the block says `enforced: false` and why (`capability_not_enabled`,
`no_platform_link`, `platform_unreachable_or_refused`, `platform_sync_disabled` when
`OPENSHARD_PLATFORM_SYNC=off`). A `.openshard/config.yml` that cannot be parsed is refused before any
work starts, because whether it holds a budget is then unknowable. The block appears in `osn run --json`
and the full receipt (`BUDGET` section). It is not yet part of the `history --json` / Platform
sync projection, whose key set is fixed by the Platform contract.
