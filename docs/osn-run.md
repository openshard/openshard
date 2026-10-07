# `openshard osn run`

A bounded, policy-gated coding task with verification performed by OpenShard.

```
openshard osn run "Implement slugify in slug.py" \
  --verify-cmd "python -m pytest -q tests" \
  --context-file slug.py --context-file tests/test_slug.py \
  [--model M] [--escalate-model M2 ...] [--max-attempts 2] \
  [--task-id task_...] [--promote] [--yes] [--no-learning] [--json]
```

## Flow

task -> isolated copy -> model turn: choose bounded actions -> OpenShard validates and performs
each action -> result shown to the model -> next turn ... -> verification run by OpenShard ->
bounded retry / escalation -> receipt.

By default (`--loop agent`) the model works in bounded turns (`--max-turns`, default 12, hard cap
30). Each turn it replies with a JSON list of typed actions and a short note; OpenShard performs
them in order inside the isolated copy and shows the model only their results on the next turn:

| Action | What OpenShard does | Authority |
|---|---|---|
| `list_files`, `read_file`, `search_repo`, `get_diff` | reads the isolated copy (bounded output); `get_diff` shows the model's own changes against the repository | path safety; protected paths (secrets, `.git/`, `.openshard/`) are refused |
| `write_file` (complete new content) | path safety, the file-mutation policy (deny / ask / allow, plus organisation write-path patterns), the agent budget, then the native `write_file` tool, which re-checks path and policy itself and records before/after hashes, sizes and line counts | a refused write ends the attempt and the run is `blocked`, exactly as a refused one-shot proposal is; nothing is retried |
| `run_verification` | runs `--verify-cmd` in the isolated copy (at most 2 per attempt) and shows the outcome and a short output tail | the model never chooses the command; each launch counts toward the command budget |
| `finish` | ends the attempt | if files changed after the last verification, OpenShard runs it once more |

A reply that is not a valid action list is re-asked once (its spend is recorded), then the run
ends in `error`. A turn may carry at most 8 actions; anything after `finish` is ignored.
`--loop writes` keeps the original behaviour: one whole-file proposal per attempt.

## Roles: planner, executor, verifier

`--roles auto` (default) can put three roles on one run; each is recorded with its own model,
provider, calls, tokens, cost (with provenance) and duration, and a role that did not run says
`skipped` and why.

| Role | What it does | When it runs (`auto`) | Model |
|---|---|---|---|
| planner | up to 3 read-only turns in the isolated copy (writes and verification requests are refused), ending in a short plan: summary, likely files, steps, what verification must show | the task's routing category is `complex` or `security`, or the repository has more than 12 files; a trivial task pays for no planner | `--planner-model`, else Routing V2 over the run's candidate pool (`deep_reasoning`) when the capability applied, else the native planner tier if the catalog knows it, else the executor's model |
| executor | the turn loop above, with the plan as advisory context | always | as before (`--model`, Routing V2, or keyword routing, with the escalation ladder) |
| verifier | one call after an attempt OpenShard itself verified: task, plan, a bounded diff, the verification evidence; replies `pass`, `warn` or `fail` with concerns | the same non-trivial rule as the planner, and a model other than the executor's is available (`--verifier-model`, Routing V2's `verifier` class with the executor's model excluded, or the native validator tier when the catalog knows it); a self-review is not paid for in `auto` | as listed; `--roles full` runs it even on the executor's own model and records `independent: false` |

The verdict is **model-reported evidence** beside the deterministic result and never changes the
verification status. A `fail` buys at most one bounded executor recovery attempt on the same
model (no escalation: nothing failed deterministically) whose result is verified like any other;
if it does not verify, its changes are undone and the verified state stands
(`recovery_outcome: reverted_to_verified_state`). At most two reviews per run. `--roles executor`
disables both roles.

The Receipt carries `osn_loop.roles` (per-role status, reason, requested and reported model,
provider, calls, turns, tokens, cost, `cost_source`, duration), `osn_loop.plan`, `osn_loop.reviews`
(verdict, summary, concerns, whether a recovery was requested and how it ended), `stage_runs`
(planning / implementation / review, the per-stage usage every Receipt surface already shows) and a
`tier_dispatch_receipt` whose `*_model_actual` fields are set only for a role that really called a
model, so `openshard history` shows which roles were dispatched. The run's `execution_model` stays
the executor's. The full local Receipt gets `ROLES`, `PLAN` and `REVIEW` sections; the hosted
projection is unchanged until the Platform contract learns these blocks.

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
| Proposed writes and every other declared action (`osn_loop.attempts[*].actions`: kind, repo-relative target, short intent, role, model, turn) | agent-declared |
| What OpenShard decided and saw for each action (policy decision, approval, whether it executed, before/after hashes and line counts of a write, counts of a read, the outcome of a verification) | OpenShard-observed; tool output, file contents and prompts are never stored |
| Model calls (`osn_loop.model_calls`: attempt, turn, role, requested and reported model, tokens, cost, `cost_source`, duration) | provider-reported usage; a cost is `provider_reported` only when the provider itself stated it, otherwise OpenShard's list-rate arithmetic labelled `list_rate_estimate`, and the run's `cost_provenance` says which |
| Policy decisions, file effects | OpenShard-observed; every proposed write is stored as an allow / ask / deny `policy_decisions` entry (with whether an approver granted an ask), and an `approval_receipt` says what approval was needed and whether it was given, so `history`, failure classification and trust scoring treat an OSN policy block as a policy block |
| Verification (exit code) | OpenShard-observed (`directly_observed` / `openshard_executed`) |
| Model cost | recorded only when the provider reported it; otherwise unknown |
| Failing test ids | OpenShard-observed: pytest node ids / jest-vitest test files the verifier named on a failed attempt; identifiers only, never output |
| Learning (`learning`) | which prior signals were consulted and what they influenced; see [learning.md](learning.md) |

A verifier that cannot be started is recorded as `not_run`, never as a pass or a model failure. A
verifier that rewrites the files it is checking does not count as a pass. The Shard entry
(`executor: osn_loop`) stores no task text beyond the usual sanitised task. The verifier is
stored as its executable name, plus, in the `learning` block, a fingerprint and a label that
shows the arguments only when every token is plain (no quotes, shell syntax, absolute paths or
secret-like values). A routing provenance block lets `openshard stats routing` include these
runs. The model that ran is chosen by you (`--model`) or by keyword routing, unless the
`adaptive_routing` capability applies (next section).

## Learning

Unless `--no-learning` is given, the run first consults evidence-backed learning signals derived
from this repository's earlier verified runs, and records what it used in a `learning` block. The
signals reach the model as advisory context, never as instructions. Checks that caught prior
failures are recommended, never run. With `adaptive_routing` on, Routing V2 prefers
repository- and task-scoped history when it clears the same sample gate. See
[learning.md](learning.md).

## Adaptive routing (experimental)

Only when the Platform lists the `adaptive_routing` capability as enabled for the linked
organisation, and only when you did not pass `--model`. Then Routing V2
(`docs/architecture/routing.md`) chooses the first model for the `execute` step from the live
catalog, the provider keys you have, the repository's `models` policy (including any
`dogfood_candidates` named for the requirement class, which may compete because the capability is
on), the budget's spend cap, and observed history when the sample is meaningful. Its recovery plan
becomes the escalation ladder when you gave no `--escalate-model`. The loop still climbs that
ladder only after an observed verification failure, the budget (above) still applies, and with
`supervisor_routing` on the `repair` step is re-decided at that boundary from what the run observed
(models tried, the observed failure, spend so far).

What never changes: an explicit `--model` always runs and is never substituted (the capability is
not even looked up); an explicit `--escalate-model` ladder always wins; the ladder is cut to what
`--max-attempts` (and a budget's `max_attempts`) can actually run; a `models:` policy that cannot
be parsed, no eligible candidate, no decision at all, or a selected model the chosen `--provider`
cannot dispatch all fall back to keyword routing and are recorded as `applied: false` with the
reason (`model_policy_invalid`, `no_eligible_candidate`, `decision_unavailable`,
`provider_mismatch`); the capability off, unconfirmed or unreachable leaves what runs as it was.
One cost is new: a run without `--model` in a Platform-linked repository asks the Platform once,
at the start of the run, whether the capability is on (a failed read is remembered for one minute).
That one answer is frozen for the whole run and recorded as `capability_snapshot`, so a dashboard
toggle applies to the next new run immediately and never to a run already in progress. Historical
per-model success is used only when at least five independently verified outcomes exist for at
least two eligible models; otherwise the record says `history_evidence:
not_used_insufficient_observed_data` and why.

## Supervisor routing (experimental)

Only when the Platform lists the `supervisor_routing` capability as enabled, and never for an
explicit `--model` (the user's choice is not re-evaluated and the capability is not looked up).
The loop already retries only after a verification failure it observed; the supervisor
re-evaluates at exactly that boundary, before a retry, and nowhere else. Its decision function is
the recovery policy that already existed (`routing/adaptive/recovery.py`): given which models ran,
that OpenShard saw the verification fail, and what the attempts cost, it says *escalate* to a
specific model or *stop* (the plan's attempts exhausted, ladder exhausted, spend at the cap or
unknowable under a spend limit). It is never consulted when a budget would refuse the next attempt
anyway: the budget's own stop is never pre-empted.

It is *applied* only when adaptive routing applied the decision whose recovery plan it follows and
you typed no `--escalate-model`; then a `stop` ends the run (`stop_reason:
supervisor_stop:<reason>`, status `failed`, because the last observed verification did fail) and an
`escalate` sets the next attempt's model. An escalation is recorded as acted on only once the next
attempt really called that model; if the run ends first (a provider error, a budget stop) the record
says `run_ended_before_retry`. Otherwise it runs in *shadow*: every decision is recorded with
`acted_on: false` and why (`user_ladder`, `adaptive_routing_not_applied`), and the ladder runs as
before. The main thing an applied supervisor changes today is stopping a retry that would rerun
the ladder's last model with no new evidence.

The Shard entry's `supervisor_routing` block records the mode, the boundary, and each decision:
the attempt it followed, the action, the policy's reason, the recommended model, whether it was
acted on, and the evidence it had (verification status and source, attempts and models so far,
spend and whether it was known, the spend cap, the loop's and the plan's attempt caps, what the
ladder would have run next and whether the recommendation differed from it). Historical performance is not consulted. The
block appears in `osn run --json`, the full local Receipt (`SUPERVISOR` section), and the hosted
Receipt sync projection in a bounded form that omits raw candidate lists and other private detail.

The Shard entry's `routing_provenance` block gets `record_mode: applied` (compared with the model
that ran first) instead of `shadow`, and an `adaptive_routing` block records whether the decision
was applied and why not otherwise, the policy and step, the selected model, its requirement class
and promotion state, the ranking components of the models it was compared with, how many models
were eligible and rejected per reason, which discovered models would have qualified
(`shadow_candidates`), the `escalation_ladder`, where it came from (`recovery_plan` or `user`),
the `max_attempts` it was cut to, and whether history was used (`history`). `openshard stats routing` attributes an
applied run to the model the decision chose (its `escalations` column counts how often the ladder
was climbed) and excludes applied runs from the shadow-agreement rate, which only means something
for decisions that did not pick the model.
The block appears in `osn run --json`, the full local Receipt (`ADAPTIVE ROUTING` section), and
the hosted Receipt sync projection in a bounded form. The hosted form keeps the selected model,
class, policy, recovery route, shadow candidates and whether history was used; detailed rankings,
rejected counts and the full candidate set remain local. With the capability off, the shadow
provenance is unchanged except that a `--model` you passed is now recorded as an explicit choice
rather than as free routing.

## Agent budgets (experimental)

Only when the Platform lists the `agent_budgets` capability as enabled for the linked
organisation (`openshard sync connect`). Without a link, when the Platform cannot be reached or
refuses the key, or when the capability is simply not on, `osn run` behaves exactly as above and a
configured budget is recorded as *not enforced*. Nothing about the link changes: the same
endpoint, organisation and `osk_` key that receipt sync uses are read once per run and the answer
is read once at the start of each run (see `docs/platform-sync.md`).

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
work starts, because whether it holds a budget is then unknowable. The block appears in `osn run --json`,
the full local Receipt (`BUDGET` section), and the hosted Receipt sync projection. The hosted form
contains the configured limits, observed usage, whether enforcement applied, the limit reached and
the action OpenShard took; it carries no prompt, path, command line or command output.
