# Learning Loop

OpenShard turns the verified outcomes of earlier OSN runs in a repository into
evidence-backed **learning signals**. It surfaces the relevant ones to later OSN
runs as advisory context, records on each Receipt what learning did, and
measures what happened afterwards.

```
runs.jsonl -> observations -> learning signals -> relevance -> OSN context (advisory)
                                               -> Adaptive Routing V2 (scoped history, capability-gated)
                                               -> Receipt `learning` block -> `openshard learn impact`
```

It is not a vector memory, it trains nothing, and it never changes policy.

## Signals

Signals are derived from `.openshard/runs.jsonl`, the same way routing
outcomes are, and are never written back into a Receipt. For OSN runs they are
derived in the background and read from a precomputed snapshot (see
[Startup cost](#startup-cost)); `openshard learn` and the MCP tool derive them
directly. Each one has:

- a **scope**: the repository and task category (the keyword classifier the
  routing layer already records);
- a **subject**: a model, a recovery path, a check, a test, a failure category
  or a policy boundary;
- **counts**, the supporting **Receipt and Shard ids**, **strength**
  (sample size) and **freshness**.

The one-line summary is rendered from those fields. The structured fields are
the source of truth.

| Kind | What it says |
|---|---|
| `model_task_outcomes` | how often a model passed verification on the first attempt for this kind of task, how many runs needed a retry, and cost per verified success when every run's cost is known and there are at least 3 |
| `recovery_path` | after model A failed verification, how often the later attempt (model B) passed |
| `recurring_check_failure` | a verify command that caught failures on similar work |
| `recurring_test_failure` | a test OpenShard saw fail in its own verification, and whether those runs later passed |
| `recurring_failure` | runs of this kind that ended the same way (verification failed, verifier could not run, timeout, policy block...) |
| `policy_boundary` | writes under an area that required approval or were denied |

Evidence rules:

- Only outcomes OpenShard observed, or an independent system verified, count as
  a pass or a failure. Agent-reported and imported outcomes are excluded and
  counted as such (`openshard learn signals` shows how many).
- Samples are distinct Receipts. They are never grouped by `shard_id`, because
  hook shard ids collide across machines.
- Missing cost stays unknown and is never treated as zero.
- A failed check shows the check failed. It does not show that the model caused
  the failure. Summaries state observations ("passed on the first attempt in 2
  of 3 recorded runs") and never rankings.
- Strength: 1 Receipt is `anecdotal` and is never surfaced; 2 is `weak`; 3–4 is
  `moderate`; 5 or more is `strong`. For check and test signals, strength counts
  the runs showing the failure, not every run observed.
- Freshness: `fresh` is 30 days or less, `aging` is up to 90 days, and `stale`
  (older) is never surfaced.

## Relevance

Retrieval is deterministic and every selected signal says why, for example
`same_repo, same_task_category, task_terms:dashboard,layout,
repeated_verified_outcome, moderate_sample, recent`.

- Model and recovery signals qualify on the task category alone.
- Check, test, failure and policy signals also need shared task terms or a file
  area named in the task (or two shared terms), so a broad category never pulls
  in an unrelated check.
- At most 5 signals are surfaced, and at most 2 of any kind.

## In an OSN run

Before routing, `openshard osn run` consults the signals for the task (turn this
off with `--no-learning`):

1. Relevant signals go to the model in an `<openshard_history advisory="true">`
   block. It comes after the task and repository listing and before this run's
   own failures. A system note says it never overrides the task, repository
   policy or rules. Policy, path safety, explicit `--model` and
   `--escalate-model`, and budgets are unaffected: the write gate never sees
   learning.
2. If a relevant `recurring_test_failure` names a test file that exists in the
   repository, that file (at most two) is shown to the model as untrusted
   content, like a `--context-file`.
3. With the `adaptive_routing` capability on, Routing V2 first tries verified
   history from **this repository and task category**. It uses that history only
   when it clears the existing gate (5 verified outcomes per model, 2 models with
   evidence). Otherwise the decision is exactly what harness-wide history would
   give. The routing record says which scope decided and why.
4. A verify command that caught failures on similar work, but is not the one
   supplied, is **recommended, never run**.
5. Each model sees only its own statistics. A `model_task_outcomes` signal goes
   only to the model it describes, and a `recovery_path` only to the model that
   recovered. When an escalation moves to another model, that attempt gets the
   context built for that model.

The Receipt gets a compact `learning` block containing:

- the status, the signal ids used and why each was selected;
- whether context (and which files) reached the model;
- `routing.influenced` (true only when an applied V2 decision used history);
- `verification.influenced` (always false; recommendations are advisory);
- the model each attempt requested, and a privacy-safe identity of the verify
  command;
- `snapshot`: the precomputed snapshot the run read (its id and when it was
  generated) and how the lookup went (`lookup_ms`, `budget_ms`, `status`).

When escalation shows different models different signals, the block lists every
signal that reached a model.

The command's arguments are withheld from its label unless every token is
plain. Failing test ids are kept on the attempt's verification record as
identifiers only, never output text.

The full local Receipt shows a LEARNING section. Sync also sends a bounded
learning summary to Platform: signal IDs, context delivery, routing influence,
advisory checks and snapshot timing. Hosted Receipts show this evidence when
present. Older Receipts remain unchanged. Runtime-reported context delivery
does not prove that the agent followed it or that it improved the outcome.

## Startup cost

OSN does not read history at startup. After every successful write to
`runs.jsonl`, a background worker re-derives the signals and the routing history
(harness-wide, and per task category for this repository) and publishes them as
one file under `.openshard/learning-cache/`. A run reads that file once, within
a budget, and uses that one frozen result for routing, the model's context and
the Receipt.

- Budget: `learning.lookup_budget_ms` in the repository config, 25 by default
  and at most 100. No remote call, no git subprocess, and `runs.jsonl` is not
  read. The budget is best effort: a result that arrives late is reported as
  `timeout` and not used.
- Size: a snapshot is at most 384 KB. A larger derivation is trimmed to fit
  (weakest and stalest signals first); the Receipt's `snapshot` block carries
  `trimmed`, `signals_stored` and `signals_derived`. If even an empty snapshot
  cannot fit, learning is reported `unavailable`.
- A late lookup fails open as `timeout`. A missing, unreadable, corrupt,
  incomplete, incompatible or oversized snapshot, or one derived for another
  checkout (`repo_mismatch`) or before a git config change
  (`identity_changed`), fails open as `unavailable`. Both mean the history was
  not read, not that there is none: the Receipt records `signals_considered` as
  unknown (`null`), never 0, and the routing record says `history_timeout` or
  `history_unavailable`, never `no_history`. Only a repository with no history
  at all (no snapshot and no `runs.jsonl`) is `no_history`.
- Freshness is recomputed from each signal's last-seen time when the snapshot
  is read, so an older snapshot never surfaces stale signals.
- The history write itself is complete and durable before the worker is
  triggered. Many writes coalesce into one worker, and a snapshot is published
  only whole (temp file, then atomic replace).
- The worker is started with Python's safe-path mode (`-P`) from the
  OpenShard home directory, never from the repository, so a cloned repository
  cannot put its own code on the worker's import path.
- `OPENSHARD_LEARNING_WORKER=0` turns background refresh off; OSN then reports
  learning as `unavailable` or serves the last published snapshot.
- `--no-learning` skips the snapshot entirely; routing then reads harness-wide
  history as it always has.

## Inspecting

```bash
openshard learn signals                    # signals for this repository, by task class (--all: include anecdotal/stale)
openshard learn signals "update the dashboard layout"   # what OSN would supply for a task, and why
openshard learn inspect ls_9a779064fd35    # one signal: counts, freshness, supporting Receipts
openshard learn last                       # what learning did on the latest OSN run, and its outcome
openshard learn impact                     # outcomes of runs with / without learning (observational)
```

Every command accepts `--json`.

Coding agents connected to the local MCP server (`openshard mcp install claude`)
can call `learning_signals(task)`, which returns the same signals with their
reasons, suggested test files and the advisory block. OpenShard cannot observe
whether an external agent acted on them, so nothing is recorded on those
agents' Receipts.

## Automatic context for Claude and Codex

Opt a repository into a separate native prompt hook:

```bash
openshard learn install claude
openshard learn install codex
```

The installed `UserPromptSubmit` hook runs `openshard learn hook <agent>` before
each prompt. It reads one bounded local learning snapshot, selects relevant
verified signals and emits the agent's documented `additionalContext` JSON.
It never blocks the prompt, runs recommended commands, chooses the model or
changes repository policy. Ordinary capture hooks remain silent. Native hooks
must be enabled and reviewed in the agent; installing files cannot bypass that
review or enable hooks on unsupported cloud/mobile surfaces.

The default lookup budget is 25 ms. Missing or unusable snapshots fail open with
no context; existing history is not read synchronously. A background worker is
nudged to refresh the snapshot for a later prompt. There is no remote history
lookup or model API call. Existing freshness, relevance, sample-size and
verification-evidence gates still apply.

After stdout is successfully written, a bounded local sidecar records signal
IDs, selection status and snapshot provenance. The last handoff in the current
capture segment is included when ordinary capture folds the next Receipt.
`context_delivery: hook_response_emitted` means OpenShard wrote the native hook
response. `context_supplied` remains false: model consumption was not observed.
The app labels the handoff accordingly. Following a recommendation and causing
an improved outcome are not established. A broken stdout never records a
handoff, and an earlier resumed segment cannot populate a new Receipt.

No prompt, transcript, summary text or model response is stored in the sidecar.
Existing ended Receipts remain unchanged. Capture and learning hooks are
separate; without capture there is no finalised delivery Receipt. Hosted usage
telemetry does not substitute for verified learning history.

Remove only the learning hook with `openshard learn uninstall claude` or
`openshard learn uninstall codex`. Other native settings and capture hooks are
preserved.

Protocol references:
- https://code.claude.com/docs/en/hooks
- https://developers.openai.com/codex/hooks

## Measuring

`openshard learn impact` puts OSN runs that used learning next to runs that
did not, and next to runs recorded before learning existed. It reports verified outcomes,
first-attempt passes, retries, cost per verified success and duration, plus
the later runs each signal was given to. Rates appear only with 5 or more
observed outcomes. This comparison is **observational**. Tasks that found
relevant history are not a random sample, so a difference is a lead to
investigate, never evidence that learning caused it.

## Privacy

Signals hold:

- counts and enum tokens;
- model ids;
- repo-relative directory areas;
- content words from the already-sanitised task, with secret-like and
  absolute-path tokens dropped before tokenising;
- test ids and ids.

They never hold prompts, model replies, command output, error messages,
absolute paths or secrets.
