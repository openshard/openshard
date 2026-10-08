# Changelog

All notable changes to OpenShard are documented here.

## Unreleased

### Added

- `openshard osn steer <run id> "note"` adds an operator note to a running OSN run, shown on
  the next planner or executor turn; `--stop` ends the run before its next model call with the
  attempt checkpointed so `osn resume` can continue it. Notes are advisory and change no policy;
  the Receipt records that a note was shown (attempt, turn, size, hash), never its text.
- Every OSN role that takes turns is shown, on its first turn, a bounded map of the
  repository observed from its files: directories with counts, file roles by fixed path
  rules, and the top-level definitions of source files (at most 60 files, 12 names each,
  6,000 characters; vendored trees skipped; a cut map says so). It replaces turns spent
  reading files to learn what they hold. The Receipt records counts only under
  `osn_loop.repo_map`.
- The OSN verifier role receives the repository's `AGENTS.md` / `CLAUDE.md`
  block, as the planner and executor do, so a review can check conventions.
  This change was made by OSN itself on its own repository (Receipt
  `rcpt_24cd298e…`, verified, reviewed and committed with the verification
  bound to the commit).
- A plan file too large to show whole is shown to the OSN executor on its
  first turn as a line-numbered outline (where each `def` / `class` /
  `function` starts; Python, JavaScript and TypeScript; at most four files,
  120 definitions each), so it reads the range it needs with `read_file`
  instead of paging through the file. The Receipt records which files were
  outlined under `osn_loop.plan_outline_files`.
- The files the planner names as likely to change are shown to the OSN
  executor on its first turn, as `--context-file` does: existing, within the
  context-file size cap, not already supplied, at most four. The Receipt
  records which under `osn_loop.plan_context_files`. The planner had already
  read them; the executor then spent its own turns rediscovering them.

### Security

- **Verifier credential scrub.** The OSN verify command, which may run agent-written code, no
  longer receives the OpenShard Platform credentials, `*API_KEY` / `*APIKEY` variables (model
  providers) or a listed set of VCS, package-registry and cloud credentials. This is a
  least-privilege scrub of those classes, not process isolation. See `docs/osn-run.md`.

### Fixed

- **`openshard doctor` no longer calls a configured agent "Ready" before it has captured
  anything.** Installed hooks are structural evidence only: a hook command that never runs
  (`openshard` not on the PATH of the process that launches the agent, a sandbox that drops it, an
  agent that skips hooks until trusted or approved) leaves no trace, and a capture service that
  refuses the hooks looks healthy. The `Capture verified` line that OpenCode and Grok Build already
  had now appears for Claude Code, Codex, Cursor, Google Antigravity and Hermes Agent too: green
  once a session from that agent is recorded in the repository's history, otherwise
  `Configured but unverified` with what to check for that agent. `doctor --json` carries
  `capture_observed` per agent (and under `claude_code`).
- **The context an agent is given about prior work no longer calls an agent-reported pass
  "passed".** `relevant_context` (the local MCP tool, `openshard context`) took the Receipt's flat
  verification token, so a Claude Code session whose hook merely relayed the agent's own
  `pytest` exit code read `Status: Passed | Verification: passed`, and a later `openshard verify`
  re-run was ignored. It now reads the shared verification truth: an agent-reported pass is
  `unknown` and named as the agent's claim (`Not verified by OpenShard (agent reported 1/1
  passed)`), a later OpenShard re-run or CI verdict describes the current outcome with its commit,
  and a hook session's status is its turn status, not `Passed`. The context text names who vouches
  for each token. Native runs OpenShard verified itself read as before.
- **A later verification now reaches the Status row and `openshard history`.** After
  `openshard verify` re-ran a Shard's checks (or CI reported on its commit), `openshard last` still
  printed `Status  Turn completed (unverified)` two rows above `Verified  Passed (OpenShard re-ran
  the check(s) ...)`, and the `history` row kept saying `(unverified) · checks: not run` with no
  sign of the re-run. Both now read the same interpretation as the Verified row: the turn status
  becomes `Turn completed (verified later: passed, OpenShard re-run)` (or `failed`, or
  `independent CI`), the history checks column shows `1/1 passed (OpenShard re-run @ <commit>)`,
  and `history --json` carries `verification_truth` per row as `last --json` does. Without later
  evidence nothing changes, and the stored record is never rewritten.
- **A capture service that is not this installation's is no longer adopted.** `openshard setup`
  and the hooks took any OpenShard service answering `/health` on the port as theirs. One started
  from another `OPENSHARD_HOME` or by another user account on the same machine then refused every
  hook with `401` (its token differs) while `setup` and `doctor` reported ready, and the whole
  session's evidence was lost. Clients now present the capture token on `/health` and the service
  answers whether it accepts it; a service that does not (or is too old to say and is not named by
  this home's state file) is treated like any other program holding the port: this installation's
  own service starts on the next port and the hooks are written for it. `doctor` and
  `capture status` report such a service instead of a green check, and `capture stop` leaves it
  alone. Found by running a real Claude Code session on a machine with a second OpenShard install.
- **Hook Shard identity is finalized under the history lock.** Agent hook sessions still get a provisional `shard_id` while their live Events are buffered, but the first `runs.jsonl` persistence now remints that position-derived id from the locked file state and preserves it on later folds. Concurrent sessions in one repository can no longer share a Shard id; embedded Events and the Receipt content hash are restamped with the persisted id. Cross-repository `shard_id` collisions remain expected, so `receipt_id` remains the global Receipt identity.

- **Adaptive routing no longer resurrects a stale curated model preference when learning history cannot be read in time.** A `history_timeout` or `history_unavailable` still fails open and records that evidence was not used, but among otherwise-equal eligible candidates it now uses current provider price before the legacy role hint. Explicit model choices, class pins, policy/capability gates, promotion, requirement fit, supersession and price-band rules are unchanged.

- Static OpenRouter price fallbacks for `z-ai/glm-5.1`, `deepseek/deepseek-v4-pro` and the retired
  `minimax/m2.7` id were `~est` values 2 to 30 times below the 2026-09-25 OpenRouter snapshot; they
  now carry the snapshot's rates with the date. The fallback applies only when the catalog cache is
  absent and the provider reported no cost.
- **OSN Shard identity under the lock.** An OSN run's `shard_id` was minted from an unlocked
  line count, so two runs writing at the same moment could share one and `history`, MCP
  `get_shard` and the hosted Receipt would merge unrelated runs into one Shard. The id is now
  minted under the history lock from the file position (`shard-YYYYMMDD-NNNN`, as every other
  writer) and never repeats an id a remaining record holds; the content hash is stamped over the
  record as written. Existing records are unchanged.
- **History queries carry later verification.** `history.query` (MCP `get_shard` / `get_receipt`
  / `recent_shards` / `search_history`, `list_receipts_by_task`) now joins the latest
  `openshard verify` attestation to each Receipt, as the CLI and TUI readers already did, so
  every reader interprets the same evidence. History is never rewritten.
- **Hosted `base_commit` for historical imports** is `null`. The importer's stored commit is the
  one the session produced, not its starting HEAD.
- **Routing outcome attempt counts** are exact for OSN runs (one per attempt OSN ran) instead of
  unknown whenever a retry happened; a legacy retry flag alone still leaves the count unknown.
- **Approval evidence.** An organisation `ask` path in `--json` / `--json-events` mode (or with no
  terminal) was recorded as `refused` although nobody refused. It is now `unanswered`, and an
  approver that failed classifies as approval unavailable rather than a user denial. The `--yes`
  help text now says built-in `ask` paths are never approved inside the run.
- **Promotion** refuses a path that resolves elsewhere through a symlinked or junctioned
  directory, so a `deny` / `ask` location can't be reached under an `allow`ed alias.
- **Organisation command prefixes** now match the program, not its spelling. A Windows suffix,
  directory, case and a versioned Python name no longer bypass a rule, and a `python` rule
  matches the interpreter OSN substitutes for a bare `python`.
- A role whose calls went to more than one model (an escalation ladder, a
  supervisor re-route) now names each model, in order of first use, with its
  own calls, tokens and cost (`osn_loop.roles.<role>.by_model`; the ROLES
  section shows `GPT-5.6 Sol → Grok 4.3` and one indented share per model).
  Before, the record carried only the last model and credited it with every
  call's cost.
- The compact OSN Receipt's COST section labels its first row `Attempt 1 ·
  all roles` when more than one role made
  calls, because that figure (the run's recorded cost minus the retries') is
  everyone's spend; with only the executor it keeps the model name. ROLES
  carries the per-role split.
- The OSN executor is told when its turn budget is running out on inspection
  alone: once half the turns are spent with nothing written, every later turn
  says how many remain and asks for the change now; the last turn says only a
  change or a finish can still count. Found by running OSN on OpenShard's own
  repository, where an executor spent all twelve turns reading. The attempt
  still ends at `--max-turns` exactly as before.
- OSN reads the verification command's output as UTF-8 (replacing
  undecodable bytes) on every platform. On Windows the locale codec (cp1252)
  raised inside subprocess's reader thread on a single undecodable byte in a
  test's output, the output was lost and the run reported the check as
  failed: an environment failure recorded as the change's. Found by running
  OSN on OpenShard's own test suite.
- An OSN Receipt's content hash is stamped last, over the record as written.
  The run entry was stamped when built and then gained the fields only the
  run knows at the end (verification command source, project instructions,
  repository state, a resume, the commit OpenShard created), so a record
  nobody edited read as `Checksum mismatch (record edited after it was
  written)`. Receipts written before this fix keep their stored hash and
  still report a mismatch; they were not tampered with.

- The OpenRouter client returns an empty string instead of `None` when a
  reasoning model spends its whole output budget thinking and the API reports
  `content: null`.

### Added

- The OSN Receipt says where the changed files were when it was written:
  `Files modified N` now carries `in an isolated copy · not applied when this
  Receipt was written` or `applied to the repository (· N skipped by
  policy)`. The run entry records it under `osn_loop.repository` (applied,
  files applied and skipped, how) and the local projection keeps counts only.
  Older Receipts without the record say nothing rather than guess.
- Ctrl-C during an OSN run with parallel workers, candidates or explorers
  now stops them: no worker starts another turn once the run is cancelled
  (its model call in flight finishes, nothing else starts), queued workers
  never start, and the interrupt reaches the run at once so the checkpoint is
  marked interrupted, instead of waiting for every worker to finish. A
  cancelled worker is recorded as `failed` / `cancelled` and the progress
  stream carries a `cancelled` event naming the worker and turn.
- `openshard osn diff <osn-id>`: the unified diff a completed run's verified
  result would make against the repository as it is now, read-only, before
  `osn apply`; it names why a result is no longer applicable (HEAD moved, a
  target file changed) and refuses for runs that kept no verified result or
  were applied already (`git diff` shows those). The checkpoint of a completed
  run now records its outcome (`result`: status, stop reason, verification
  state, attempts, changed files), so `openshard osn runs` shows a finished
  run's real attempt count instead of the number of attempts it could have
  resumed from.
- `openshard osn run --json-events` (and `osn resume --json-events`): the
  run's progress events as NDJSON on stdout, one `{"event", "seq",
  "elapsed_s", "data"}` object per line as they happen, ending with
  `{"event": "result", "data": <the --json object>}`; a Ctrl-C ends the stream
  with an `interrupted` event naming the run to resume. It is the same engine
  callback the terminal renderer consumes, never prompts, and writes nothing
  else to stdout. `--json` and text output are unchanged.
- OSN reads the repository's `AGENTS.md` and `CLAUDE.md` (the maintainers'
  instructions for coding agents) and gives them, bounded (8,000 characters
  per file, 12,000 in all, truncation marked), to every role on every turn
  inside a `<project_instructions>` block; the system prompts describe it as
  guidance to follow where it does not conflict with the task, the action
  contract or OpenShard policy, and say it never grants authority. The run's
  header lists the files (`Context AGENTS.md (2.1 KB) shown to every role`)
  and the Receipt records them under `osn_loop.project_instructions` (path,
  bytes, sha256, truncated, chars shown): that the agent was given them,
  never that it followed them.
- `openshard osn apply <osn-id> [--commit] [--yes] [--json]`: put a completed
  run's verified result into the repository later, without re-running the
  model. A verified run that was not promoted keeps exactly the bytes
  OpenShard verified under its checkpoint (`verified_files`, the Receipt's
  hashes; `base_files`, what the repository held at those paths). Applying
  goes through the same file-mutation policy gate as `--promote`, re-checks
  the hashes, logs a sandbox-apply receipt and marks the checkpoint
  `applied`; `--commit` commits exactly those files and binds a
  re-verification to the commit through the post-session path. It refuses,
  naming the rule, when the run did not complete, did not verify or was
  promoted, was applied already, HEAD moved, a target file changed since the
  run, or the kept bytes no longer match. In an interactive terminal a
  verified, unpromoted run now ends by asking once whether to apply; piped
  and `--json` runs are told the command. `openshard osn runs` shows each
  completed run's result state.

- `openshard osn run` is now an iterative agent loop by default (`--loop agent`).
  Instead of one whole-file proposal per attempt, the model takes bounded
  turns (`--max-turns`, default 12) choosing typed actions from what the
  previous actions returned: `list_files`, `read_file`, `search_repo`,
  `get_diff`, `write_file`, `run_verification` and `finish`. OpenShard decides
  whether each action may happen (path safety, the file-mutation policy with
  allow / ask / deny, organisation write-path patterns, the agent budget),
  performs it in the isolated copy, and shows the model only the result. The
  model may request the fixed verification command a bounded number of times
  per attempt and sees its outcome and a short output tail; a verification
  that ran on the final files is the attempt's verification, otherwise
  OpenShard runs it after the model finishes. `--loop writes` keeps the
  original one-shot behaviour; the `run_bounded_loop` contract for
  write-proposing providers is unchanged.
- `write_file` is now a real native tool (`NativeToolRunner`): path-safe,
  re-checks the file-mutation policy even for an approved call, and reports
  before/after hashes, sizes and line counts, never content.
- The OSN Receipt records every declared action (kind, repo-relative target,
  short intent, role, model, policy decision, approval, whether it executed,
  observed effect, duration), per-attempt turn counts, an action summary, and
  every model call (attempt, turn, role, requested and reported model, tokens,
  cost, cost provenance, duration). The full local Receipt gets an
  `OSN ACTIONS` section; the hosted projection carries counts and model calls
  only, never paths or output.

- `openshard osn run --roles auto|executor|full`: planner → executor → verifier
  as real runtime behaviour. The planner takes up to three read-only turns in
  the isolated copy (writes refused by the harness) and ends with a short
  plan the executor receives as advisory context; it runs only when the task
  is not trivial. The verifier reviews a result OpenShard itself verified
  (task, plan, bounded diff, verification evidence) on an independent model
  when one is available; its `pass` / `warn` / `fail` verdict is recorded as
  model-reported evidence and never changes the verification status. A
  `fail` buys one bounded executor recovery attempt on the same model; if it
  does not verify, its changes are undone and the verified state stands.
  Role models come from `--planner-model` / `--verifier-model`, Routing V2
  over the run's candidate pool (`deep_reasoning`; the `verifier` class with
  the executor's model excluded), the native role tiers, or, recorded as not
  independent, the executor's model. The Receipt names every role's model,
  provider, calls, tokens, cost (with provenance) and duration, says
  `skipped` and why for a role that did not run, and gains `stage_runs`
  (planning / implementation / review) and a `tier_dispatch_receipt` so
  existing surfaces show which roles were dispatched.

- The Receipt of a multi-agent OSN run records its agent graph
  (`osn_loop.agents`: every agent with role, model, status, usage, cost and
  outcome, and the edges between them), derived from the recorded roles,
  workers, candidates, synthesis and reviews; the full local Receipt shows an
  `AGENTS` section. The learning loop derives two new signal kinds from
  OpenShard-observed runs: `topology_outcomes` (verified runs, cost per
  verified success and the extra cost of parallel agents per topology) and
  `agent_model_outcomes` (per model as a worker or candidate: usable
  results, action-contract failures, own-copy verification, wins).

- Parallel candidates (`--topology candidates`): the whole task on up to
  `--max-workers` distinct models at once, each in its own isolated copy;
  OpenShard runs the verify command in every candidate's copy and ranks them
  by a fixed order (verified, fewest refused writes, usable outcome, fewest
  files, cheapest, fewest turns, order); the winner is synthesised into the
  run's copy and verified again, losers only reach the Receipt
  (`osn_loop.candidates`: policy, every candidate's rank and evidence, the
  winner, losers' cost). No verified candidate hands the task to the
  executor with an advisory.

- Durable OSN run state and `openshard osn resume`: every run checkpoints
  under `.openshard/osn-runs/<osn-id>/` (plan, roles, finished attempts
  with verification results, changed files' bytes, model calls with cost
  provenance, budget counters, repository fingerprint) at each loop
  boundary; Ctrl-C marks it interrupted, a written Receipt marks it
  completed. `osn resume` continues from the last finished attempt in a
  fresh isolated copy with the same task, verify command, options and model
  ladder, carrying the earlier calls and budget into the Receipt
  (`osn_loop.resumed`, attempts flagged `resumed_from_checkpoint`), and
  refuses by rule when the run completed, its process is alive, the
  repository changed, the verify command differs or the checkpoint cannot be
  read. `osn runs` lists checkpoints and their resumability.

- Parallel writing workers (`--topology auto|single|roles|parallel`,
  `--max-workers`, default and hard cap 3): a non-trivial run whose planner
  proposes independent subtasks with disjoint write scopes (validated by the
  harness: at most three, scopes disjoint and safe, dependencies acyclic)
  runs each subtask on its own worker in its own isolated copy, on its own
  routed model (distinct models when routing offers them), with a scoped
  write authority on top of the file-mutation policy. Synthesis copies the
  workers' non-overlapping files into the run's copy, surfaces conflicts,
  out-of-scope files and missing required work to the executor as a bounded
  advisory, and the result is verified by OpenShard like any other attempt.
  The Receipt records the topology decision (requested, selected, reason,
  worker count, expected and actual extra cost), every worker (status,
  model, scope outcome, usage, cost provenance, own-copy verification,
  actions), the synthesis outcome, `implementation_models` and an
  `economics` block (cost by role / worker / model / attempt and
  `cost_per_verified_success`). `execution_model` is the executor's when it
  ran, else the first worker's; a planner's or verifier's model is never
  reported as the execution model, and an executor that was not needed is
  recorded `skipped (workers_synthesised_cleanly)`, not failed.

- Bounded parallel read-only exploration for the planner (`--explore`, default
  on): the planner may hand up to three independent questions to exploration
  workers, each a read-only agent turn loop on the isolated copy (at most two
  turns, writes refused), at most three at once, on a fast control-plane
  model when routing offers one. Their compact findings come back to the
  planner as observations; the planner remains the single reasoning owner.
  Each worker is recorded on the planner's role record and in `model_calls`
  with role `explorer`, with its own model, usage, cost provenance and
  duration.

### Changed

- `openshard osn run` no longer requires `--verify-cmd`. Without it the run
  uses the repository's verification contract (`verification_commands` in
  `.openshard/config.yml`), else the test command OpenShard detects for the
  repository, and refuses to start when none is known (OSN never reports work
  verified without a check it ran itself). A configured or detected command
  that the command-safety classifier would not run silently is refused with
  the reason; `--verify-cmd` remains the user's explicit authority. The
  run's header, the Receipt (`osn_loop.verification_command`: label and
  source `user` / `config` / `detected`) and the checkpoint name where the
  command came from.

- `openshard osn run` shows more of what is happening while it happens: the
  planner's plan as the executor received it (summary, steps, files, proposed
  subtasks); each verification's failing test ids and the last lines of the
  command's output when it did not pass (and whether the verifier could not
  run or rewrote the files it checked); checkpoint write failures and attempts
  that ended on an unusable reply; and, at the end, the Receipt id, the
  `openshard last` pointer and the run id. Progress events from parallel
  workers and candidates now carry `worker_id` / `subtask_id`, and the
  renderer prefixes their lines with it so concurrent workers never read as
  one agent. The `verification_result` event carries `failed_tests`,
  `setup_failure`, `tainted` and (never on a pass) a redacted `output_tail`.

- Model roster: `deepseek/deepseek-v4.1-flash` is the curated cheap default
  (routing class `cheap_coding`, Ask Mode, the `cheap` tier fallback and the
  boilerplate scoring preference). `deepseek/deepseek-v4-flash`, the 0423
  snapshot OpenRouter still serves under the old id, is deprecated: kept for
  history and explicit selection, never chosen as a routing default.

- Model cost provenance is explicit: `UsageStats.cost_source` says whether a
  call's cost is the provider's own figure or OpenShard's list-rate
  arithmetic; the OpenRouter client asks for usage accounting so the
  provider's cost is recorded when it is returned, and labels its fallback
  arithmetic as an estimate. An OSN run's `cost_provenance` is
  `provider_reported` only when every call's cost was, otherwise
  `official_rate_estimate`; an unknown origin is not claimed.

## 0.4.14 - 2026-10-06

<!-- release-title: Openshard v0.4.14 - Cloud Capture -->

### Fixed

- Windows release validation now uses a platform-native synthetic Codex
  transcript path. The production Codex transcript validator was already
  correct; the hard-coded Linux test fixture was what failed on Windows.
- Automated immutable version tags now explicitly dispatch the existing
  release workflow. GitHub does not start a new push-triggered workflow when
  a tag is created with the repository GITHUB_TOKEN, so the previous
  tag-only handoff could stop before build and PyPI publishing.

### Included

- This patch includes the full 0.4.13 cloud-capture candidate: secure
  proxy-injected Claude Cloud credentials, first-hook repository identity,
  live Codex model/provider/token usage from a matching runtime transcript,
  and the connected-capture foundations used by the hosted Claude, Codex and
  Cursor setup flows.

## 0.4.13 - 2026-10-06

<!-- release-title: Openshard v0.4.13 - Claude Cloud Connected Capture -->

### Fixed

- Connected cloud capture now carries repository identity and branch from the
  first hook when git can identify them, instead of waiting for a later
  Receipt fold. This keeps the hosted Remote attached to the repository from
  the start; Platform can still self-heal when identity arrives later.

- An external agent's record that carries only the old pass/fail flags (no
  capture detail) no longer reads as "Passed (OpenShard ran the check(s))".
  Its outcome is shown as the agent's own report, as for every other
  hook-captured session.

- Receipts for external or historical runs (Claude Code, Codex, Cursor,
  OpenCode and other observed agents, and imported history) no longer show
  OpenShard control evidence, even when the stored record carries it: no
  policy decisions, approvals, permissions, sandbox, budgets, organisation
  policy, capabilities, adaptive/supervisor routing or OSN loop. This covers
  local Receipts, hosted projections, Events, provenance, learning signals,
  Insights and PR comments. OpenShard only observes these agents and cannot
  block them, so the full Receipt now says so in its POLICY section. Stored
  records are not rewritten, and their checksum is still verified as written.

- A Receipt whose integration cannot see file changes (Grok Bot's Action
  Recording export) no longer reads as "no files changed" outside the
  Receipt itself. The proof contract reports `actions` as
  `partial / file_changes_not_observable` and `files` as
  `unknown / not_observable` instead of `present / no_file_changes`, and
  `openshard pr comment` shows "not observable" instead of `0`. The PR
  comment JSON keeps `files_changed` and adds `files_observable`.

### Added

- Live Codex hook capture can now use the hook-provided runtime transcript as
  bounded usage telemetry. After the transcript proves the same session id,
  Openshard reads only model/provider identifiers and the latest cumulative
  `token_count`, separates cached from uncached input, and records token
  provenance as vendor telemetry. A dated list-rate cost estimate is added
  only when one known priced model served the cumulative usage; otherwise
  cost stays unknown. Transcript content and the locator never enter the
  Receipt or sync payload.

- Connected capture supports provider-managed credential proxies without
  putting the real `osc_` secret in the agent runtime. Claude Cloud can use
  the fixed `OPENSHARD_CONNECTED_TOKEN=proxy-injected` marker; hosted
  sandboxes whose vault supplies its own opaque placeholder can set
  `OPENSHARD_CONNECTED_CREDENTIAL_MODE=proxy`. Proxy mode rejects real
  `osc_` / `osk_` values so an accidentally exposed Openshard token fails
  closed. A missing or wrong proxy injection is still reported as the
  Platform's own 401/403, never as success.

- A Receipt honesty eval (`python -m openshard.evals.receipt_honesty`). It
  produces Receipts through real agent capture, the OSN loop and
  `openshard verify`, then checks that every local and hosted surface claims
  only what the evidence supports: agent-reported passes stay unverified,
  failures are never hidden, missing cost and tokens stay missing, and free
  and paid runs get the same evidence. It tests Receipts, not models.

- Usage and cost evidence on the same Receipt (`openshard last`,
  `openshard usage show`, `.openshard/usage.jsonl`). Cursor hooks still
  capture no tokens or cost; Grok Bot OpenTelemetry tokens stay
  vendor-telemetry. Later Cursor usage (Cloud Agents `GET /v1/agents/{id}/usage`
  and Admin usage events) can be reconciled onto the existing Receipt by
  `openshard usage reconcile`. An all-zero Cloud Agents response without
  `usageUuid` is pending, not observed $0. Hosted copies get a separate
  `usage-evidence` route; the receipt-sync payload is unchanged.

## 0.4.12 - 2026-10-05

<!-- release-title: Openshard v0.4.12 - The Next Run Release -->

### Learn from the last run

You can now give Claude Code and Codex relevant history before their next prompt.
Openshard supplies advice through the agent's native prompt hook and records the
handoff in its next captured Receipt.

- Local hooks use a small saved snapshot of verified history.
- With `--hosted`, a fresh cloud checkout can use hosted Receipts from the same
  repository. It needs an existing organisation connection with read access.
- Receipts show that the context was delivered, with links to the earlier
  Receipts behind hosted advice.
- If history is missing, access is unavailable or the lookup times out, the
  task continues.

### Opt in

```sh
pip install --upgrade openshard==0.4.12
openshard learn install claude --hosted
# or: openshard learn install codex --hosted
```

Connect your existing organisation account first and review/enable hooks in
your agent. Leave off `--hosted` to use local history. Capture setup is separate.

### What the advice means

This first hosted version highlights repeated verified check results from
similar tasks. It does not automatically run commands, select a model or change
permissions. Delivering context is observable; whether the model follows it or
improves the result still needs evidence.

### Cloud usage

The hosted platform now accepts native Claude usage logs and displays observed
session model, tokens and cost when the runtime exports them. Missing usage
stays unknown. Complete task billing and real Scribe/Tether export coverage
are still being checked; this release does not reconstruct missing past data.


## 0.4.11 - 2026-10-04

### Fixed

- Connected agent sessions keep separate upload queues. Starting another
  session no longer replaces a previous session's pending evidence.
- Receipt delivery stays within the session that captured it. Queued evidence
  cannot be sent to a different organisation after an account change.
- Post-session verification requests reach every connected session for that
  repository, rather than whichever session status happened to select.

### Added

- `remote status` shows persistent connected sessions and their queued evidence,
  instead of incorrectly calling an active account connection unattached.
- Hosted Receipts can show OSN's learning evidence: which history was supplied,
  whether it changed routing, and which checks were recommended. Recommendations
  remain advisory; supplying context does not prove that the model followed it
  or that it improved the outcome. Older Receipts remain unchanged.
- A runtime capture audit documents where model, token and cost evidence is
  available, and which cloud integrations still need a readable evidence source.


## 0.4.10 - 2026-10-04

### Fixed

- Release validation now installs the built wheel into a clean environment and
  checks `remote create`, `remote attach`, `workflow timeline`, and `verify`.
  The 0.4.9 PyPI distribution did not contain the newer remote-capture commands.
- Claude capture reads model identity from SessionStart and PostModelSwitch,
  and effective effort from documented hook payloads. Missing identity stays
  unknown; requested environment settings are never promoted to observations.
- `verify --compare-base COMMIT` independently runs approved pytest commands
  on detached base/head worktrees. It reports new and baseline-matching failures
  without changing the verification verdict or inferring an environmental cause.
- Explicit workflow correlation now survives authenticated capture and queue
  replay across agent adapters, with conflicting declarations counted.


### Added

- **Learning Loop V1** ([docs/learning.md](docs/learning.md)). OpenShard derives evidence-backed
  learning signals from verified OSN outcomes in a repository: model outcomes by task
  category, recovery paths, checks and tests that caught failures, recurring failure
  categories and policy boundaries. Each signal carries its sample size, freshness and
  supporting Receipt ids. Only OpenShard-observed or independently verified outcomes count;
  missing cost stays unknown and single Receipts are never surfaced.
  - `openshard osn run` supplies up to 5 relevant signals to the model as an advisory
    `<openshard_history>` block (never instructions), with up to 2 test files that failed on
    similar work. It recommends, never runs, checks that caught prior failures, and records
    a compact `learning` block on the Receipt. `--no-learning` turns it off.
  - With `adaptive_routing` on, Routing V2 first tries repository- and task-scoped verified
    history under the same sample gate, else the unchanged harness-wide history.
  - OSN verification records the failing test ids it observed (identifiers only).
  - The local MCP server gains `learning_signals(task)`, so Claude Code, Codex and other MCP
    clients get the same advisory signals; nothing is claimed about whether they used them.
  - `openshard learn signals | inspect | last | impact` inspects learning and compares
    outcomes of runs with and without it (observational, no causal claim). Hosted sync stays
    bounded to the current Platform contract until the learning projection is added there.
  - `osn run` reads no history at startup. A background worker re-derives signals and
    routing history after each history write and publishes a complete snapshot. A run does
    one bounded local lookup (`learning.lookup_budget_ms`, default 25 ms, at most 100), with
    no remote call. It freezes the result for routing, context and the Receipt, and each
    model sees only its own statistics. A late, unusable or other-checkout snapshot
    fails open and is recorded as `timeout` or `unavailable` with unknown counts, never
    as no history or zero. Snapshots are capped at 384 KB (the weakest, stalest signals
    are trimmed to fit, with the full count kept), the worker retries failed
    passes and OSN restarts a stale worker after its lookup; `OPENSHARD_LEARNING_WORKER=0`
    turns background refresh off.

- **Remote capture: evidence leaves an ephemeral agent environment while
  the agent works.** `openshard remote create --agent <agent>` (on a trusted
  machine) opens a hosted remote capture and prints a short-lived token
  scoped to it; `openshard remote attach` (in the environment) connects to
  it. From then on every hook adapter's Events are spooled locally first
  and streamed to the Platform in small batches seconds later, the session's
  Receipt and later verification evidence are delivered through the same
  token, and a runtime that is destroyed without a session-end event leaves
  a capture that reads Partial with exactly the Events that made it out. The
  environment never holds an organisation API key. See docs/remote-capture.md.
- **Verification that can become green for external agents, without rewriting
  the Receipt.** Later evidence is appended to `.openshard/verifications.jsonl`
  and joined at read time; the stored Receipt keeps what the session knew.
  - `openshard verify --ci` attaches the GitHub check-run verdict for the
    Shard's exact commit as `independently_verified` / `ci_report` evidence.
    Never for another commit, never for a dirty tree; pending or unavailable
    CI records nothing.
  - `verification_truth` now carries `history` (every piece of evidence,
    oldest first), `artifact_sha`, failed check names, and a `ci` basis. The
    newest conclusive evidence is the current state; a failure is never
    hidden by a pass.
  - `openshard last` shows work, verification, evidence source and capture
    completeness as four separate facts, with the original session record
    and the evidence history (`last --json`: `verification_view`).
  - `post_session_verify: safe` (opt-in) starts a safe-only `openshard verify`
    when a captured session closes.

- **Later verification evidence syncs to the Platform.** An already-hosted
  Receipt is never resent; `openshard sync now` and the background sync now
  also send the attestations recorded after it (`openshard verify`,
  `openshard verify --ci`) and Core's interpretation of them to the
  Platform's verification evidence route. Sent once per distinct evidence
  set; a Platform without the route is skipped quietly.

### Changed

- **Claude Code Receipts are truthful about usage, provider and outcome**
  ([docs/agent-capture.md](docs/agent-capture.md)).
  - Tokens are session totals summed from the API usage in Claude Code's
    transcript, once per message id, instead of the status line's last API
    call; without a readable transcript they stay unknown, never an
    undercount. A per-model breakdown is kept locally.
  - Claude Code's session cost is labelled `cost_provenance: agent_reported`
    (it is Claude Code's own estimate), still shown as an estimate.
  - `capture.provider` comes from Claude Code's environment (Bedrock /
    Vertex / Foundry / Anthropic; unknown behind a custom
    `ANTHROPIC_BASE_URL`) and `capture.surface` from
    `CLAUDE_CODE_ENTRYPOINT`; both are synced as `provider` / `surface`.
  - At session end capture records the end HEAD and the commits the
    session created (reflog-proven, inside the session window, corroborated
    by the agent's own git command; fast-forward pulls never count); the
    synced `commit` is the end HEAD only when it is one of those, and
    `pr_url` only when `gh` (capture service only) reports a PR whose head
    is such a commit (`OPENSHARD_PR_LOOKUP=off` disables the lookup).
  - Headless sessions without Claude Code's own cost are priced per model
    from transcript usage, with 1-hour cache writes at Anthropic's published
    1-hour rate; an unpriced model leaves the cost unknown.
  - A check command that failed and then passed on a re-run reads passed;
    the failed run stays listed and counted.
  - Resuming an ended session opens a new Receipt (`start_source: resume`,
    `capture.resumed_from_receipt_id`) instead of rewriting the ended,
    possibly already synced one, which used to leave it stale forever.
  - Tasks drop Claude Code's `<pasted_content>` wrapper; `claude-opus-5-5`
    displays as "Claude Opus 5.5".
- `openshard config set-owner "<name>" [--repo] | --clear` sets the explicit
  Receipt owner (`identity.owner`, user-global `~/.openshard/config.yml` by
  default). Hook, import and wrap captures now stamp it like native runs;
  it is never inferred from git or organisation data.
- The receipt's `Checks` row names each group (`1 passed, 1 failed, 1
  unknown`) when not everything passed. The stored and synced `checks` string
  is unchanged.
- `openshard verify` first closes sessions idle for an hour whose end event
  never arrived (`session_end_not_observed`).
- `openshard ci check` reads the current verification state, including a
  later re-run or CI verdict, like `last`, `proof` and `trust` already did.

### Fixed

- OSN verification recorded no failing test ids when pytest, jest or vitest
  printed in colour (e.g. `FORCE_COLOR=3`): ANSI escape sequences are now
  stripped before parsing. Only identifiers are kept, as before.

## 0.4.9 - 2026-09-28

### Changed

- **Routing around dynamic model candidates.** Curated `tier`, `roles`,
  `latency_class`, `experimental` and `cost_class` are now legacy advisory
  metadata (`registry.LEGACY_ADVISORY_FIELDS`): shown, kept for old Receipts
  and configs, never a routing filter for new work. Every catalog entry has a
  derived **promotion state** (`openshard/models/promotion.py`: discovered,
  eligible_for_shadow, dogfood_candidate, validated, stable, retired,
  restricted) built from provider facts and curation, and **requirement
  classes** (`openshard/routing/requirements.py`: fast_control,
  routine_coding, deep_reasoning, vision, long_context, verifier) describe
  what a step needs and resolve against the current eligible pool with an
  ordered, per-candidate-recorded ranking (promotion, observed evidence when
  meaningful, requirement fit, in-family supersession, price band, legacy
  hint, price). `models.dogfood_candidates` names discovered models an
  organisation wants Routing V2 to evaluate for a class; they never enter
  public (capability-off) routing. `models classes` shows both views.
- **Stale defaults.** `minimax/m2.7` was never an OpenRouter id and is now
  curated `deprecated`; the legacy `complex` role selects on the
  `long_context` fact (`minimax/minimax-m3`). `anthropic/claude-opus-4.8-fast`
  (not listed) moved to `watchlist`. A curated id missing from a fresh
  provider snapshot is `retired` for fresh runs (a stale cache never retires
  anything; an explicit choice is still honoured). Guard tests run every
  class against a checked-in real provider snapshot.
  See `docs/architecture/routing.md`.
- **Routing V2 for `openshard osn run`** (behind the `adaptive_routing`
  capability). `TrajectoryPolicyV2` (`routing/adaptive/policy_v2.py`) decides
  per step (`execute`, then `repair` after an observed verification failure)
  over requirement classes: explicit model, budget, observed failure required,
  escalation along the class ladder with tried models excluded, pins, then
  the requirement ranking with dogfood candidates competing and observed
  history used only past an evidence gate (5 verified outcomes per model, 2
  models). The supervisor re-decides the `repair` step inside the existing
  recovery envelope. `RoutingContext` v2 carries the trajectory (step,
  attempt, models tried, last verification, spend, cap). Receipts record the
  policy and step, promotion state, ranking components, shadow candidates
  and whether history was used; the capability-off path is unchanged.
- **Run-level capability snapshot.** An OSN run reads the organisation's
  enabled capabilities once at its start (bypassing the positive cache) and
  keeps that answer for the whole run, recorded as `capability_snapshot`.
  A Platform toggle applies to the next new run; offline still fails closed.
- **Hosted OSN control evidence.** The extended Receipt / Platform sync projection now carries
  bounded `agent_budgets`, `adaptive_routing`, `supervisor_routing` and
  `capability_snapshot` blocks. Platform can show the limits that governed a run, the
  starting route and recovery route, and supervisor actions without receiving prompts,
  paths, command lines, raw output or the full candidate ranking. Older Receipts keep the
  four fields null.
- Recovery may re-enter a class with a different model (it still never
  retries a tried model and the attempt cap still holds).

## 0.4.8 - 2026-09-25

### Added

- **Adaptive routing foundation** (`openshard/routing/adaptive/`). Routing is
  now expressed as catalog -> eligibility -> `RoutingContext` ->
  `CandidateSet` -> `RoutingPolicy` -> `RoutingDecision` -> verification ->
  `RoutingOutcome`. The deterministic baseline policy picks a routing class
  from known task facts (category, risk, read-only, capability needs) and
  selects within it using the routing classes below; it computes no scores
  and does not learn. Explicit models are never substituted, discovered
  models are candidates only when named, and recovery (cheap attempt ->
  verification -> escalate) is bounded and escalates only on an observed
  failure. Each run records the decision in shadow mode as a new
  `routing_provenance` Receipt block (candidates considered, selected
  model, reasons, policy and version, requested/resolved class, pin or
  explicit selection, fingerprints) next to the model that actually ran;
  the executed model is unchanged. Outcomes are derived from Receipts at
  read time, so existing Receipts work without migration.
- **Dynamic model catalog** (`openshard/models/catalog.py`). OpenShard now
  merges the curated registry with OpenRouter's model list (cached in
  `~/.openshard/openrouter-models.json`, refreshed when older than 24h,
  with the stale cache or curated-only list used as offline fallback) into
  one normalized entry per model: provider, canonical id, display name,
  aliases, family, status, release date, context, modalities, tool support,
  pricing with source and timestamp, capability tags, discovery source and
  routing eligibility. A newly released model is recognised, shown and
  explicitly selectable without an OpenShard release, but is never routed
  by default until it is curated.
- **Routing classes** (`cheap_coding`, `balanced_coding`,
  `frontier_reasoning`, `fast`, `vision`). The cheap/main/escalate/visual
  routing roles now select by class from curated models instead of by a
  fixed model version. Selections are unchanged today.
- `openshard models catalog` (`--refresh`, `--offline`, `--discovered`,
  `--family`, `--json`) and `openshard models classes`, which show each
  class's current model and newer same-family **promotion candidates**
  (e.g. DeepSeek V4.1 Flash for `cheap_coding`) without selecting them.
- `models.routing_classes` config pins a class to a model (an explicit
  choice, so a discovered model is allowed; deprecated pins are ignored
  with a warning). `openshard roster add`, `models.custom_roster`,
  `allowed_models` and `blocked_models` accept catalog-discovered ids and
  aliases, and `openshard models show` displays discovered models.
- **Capture Verification v2.** Each integration now records the strongest
  check outcome its current official documentation supports, and never
  overstates it. See "Verification evidence (v2)" in `docs/agent-capture.md`
  for the per-agent audit.
  - **Claude Code:** a foreground Bash/PowerShell `PostToolUse`, which is
    documented as success-only, is recorded as `passed`. `PostToolUseFailure`
    with an `Exit code N` first line is `failed`, and N is kept.
  - **Cursor:** `Shell` `tool_output.exitCode` is read.
  - **Grok Build:** `toolResult.exit_code` is read, unless the result is
    truncated.
  - All of these outcomes are `agent_reported`. OpenShard did not run the
    commands.
  - Interrupted, timed-out, denied, cancelled and backgrounded commands are
    `unknown`, never failed checks.
  - Receipts label reported outcomes on screen (`1/1 passed (agent-reported)`).
    The synced `checks` string is unchanged.
- **`openshard verify`: post-session verification.** OpenShard re-runs the
  repository's verification contract (`verification_commands`), else its
  detected test command, plus with `--from-observed` the agent's own
  observed checks.
  - Commands are classified by the native safety rules: blocked commands
    never run, and needs-approval commands run only with `--approve`.
  - OpenShard reads each exit code itself and appends a `directly_observed`
    / `openshard_executed` attestation to `.openshard/verifications.jsonl`.
  - The result is bound to the commit SHA only on a clean tree. Timeouts are
    `unknown`, never failed.
  - Receipts are never rewritten. `openshard last` shows `Re-verified: ...`
    and `last --json` carries `post_session_verification`.
  - This is evidence only: it exits 0 whatever the checks' outcome.
- **Task correlation across agent sessions.** Claude sessions can carry an
  explicit OpenShard task ID, kept through capture and storage, so several
  sessions on one task are grouped together in history and receipts.
  OpenShard never guesses that sessions belong together. Codex CLI does not
  yet pass the task ID through its project hooks (see `docs/agent-capture.md`).
- **File-mutation policy enforcement.** Files applied from a sandbox to the
  repository are checked against policy first: `allow`, `ask` (needs
  approval) or `deny` (never written, cannot be overridden). Secrets,
  `.env*`, `.git/` and `.openshard/` are denied; CI/Docker config,
  `pyproject.toml` and `package.json` need approval. `apply-last` asks when
  needed and `--yes` records the approval source. Receipts record the
  decision, approval and whether the change was applied; applying is never
  treated as verification.
- **Command policy before verification.** OpenShard-controlled command
  execution, including verification commands, goes through the same
  `allow` / `ask` / `deny` gate. `deny` never runs and `ask` runs only with
  granted approval. Receipts record the decision, approval, whether the
  command ran and its exit code; `executed` is never `verified`.
- **Bounded OSN execution loop.** OSN can run inspect -> plan -> policy ->
  isolated changes -> verification -> bounded retry -> receipt. Changes are
  applied only in an isolated copy, OpenShard runs the verifier itself,
  retries are capped at 5 and stop when there is no progress, and policy
  blocks are not retried. Isolation protects repository files, not the host
  process.
- **`openshard osn run`.** Runs a real coding task with a model provider
  (`openshard osn run TASK --verify-cmd ...`). Model output is treated as
  untrusted and passes policy before it is applied in the isolated copy.
  Verified changes can optionally be promoted into the repository through
  the policy gate. The receipt records verification (`not_run` when the
  verifier could not run), model, retries, reported cost and shadow routing.
  Adaptive routing does not select the model.
- **Outcome classification.** Each attempt records what happened, what
  caused it when that can be shown, and whether it may be used as evidence
  about a model. Environment, provider, rate-limit, timeout, policy,
  approval and unknown-cause outcomes never count against a model's coding
  quality, and a non-zero verifier exit alone does not blame the model.
  Existing records stay compatible and are not rewritten.
- **`openshard stats routing`** (`--json`). A read-only, descriptive report
  of routing outcomes grouped by class and model: runs, verified outcomes,
  retries, escalations, cost, latency and coverage. Unknown verification is
  never counted as failure. It does not rank models or change routing.

- **One verification interpretation for every surface**
  (`openshard/history/verification_truth.py`). The receipt's new `Verified`
  row, `openshard last` / `last --json` (`verification_truth`),
  `proof last`, `trust last`, the Home screen's `Verify` column, the quality
  summary and the CI policy check now read the same interpretation of the
  recorded evidence: who vouches for the outcome (`authority`), the
  strongest claim it supports (`state`) and the flat token (`effective_status`).
  The latest `openshard verify` attestation takes precedence over the
  session's own claim, which stays visible as history; the stored Receipt is
  never rewritten.
- `openshard verify --strict`: exit 1 when an executed check failed, 2 when a
  planned check could not run or nothing was planned. The default exit
  behaviour (evidence only, exit 0) is unchanged.
- **Failed commands are activity evidence.** A shell command the agent
  reports as exited non-zero -- even through the success hook -- is counted
  in `capture.command_failure_count`, listed under the receipt's `Activity`
  with its exit code and the class the existing safety classifier gives it
  (`policy class: blocked` for `rm -rf /`), and mentioned in the summary.
  It is never a verification check; `tool_failure_count` and telemetry are
  unchanged.
- Prompt excerpts and captured command text now also redact e-mail
  addresses, JWTs and PEM private-key blocks (`security/redaction.py`);
  the file secret scanner is unchanged.

### Changed

- **Agent-reported verification is no longer presented as verified.** An
  agent's own `exitCode: 0` (Cursor, Grok Build, Claude Code) was stored as
  `agent_reported` but read by `proof last`, `trust last`, the quality
  summary and the CI policy check as `passed`. It is now `unknown` for
  those consumers (`verification_state: agent_reported_passed`), rendered
  as `Verified  Not verified by OpenShard (agent reported 1/1 passed)`,
  weak proof (`partial`) in the proof contract, and the new
  `verification_unverified` trust penalty (same 20 points as
  `verification_not_run`). An agent-reported failure stays `failed`.
  `Checks  1/1 passed (agent-reported)` still shows the claim.
- **Integrity affects proof and trust.** A Receipt whose content no longer
  matches its stored checksum is an unsafe proof finding
  (`content_hash_mismatch`; `proof last` reports `unsafe` and exits 1) and
  scores 0 (`unsafe`) in `trust last` with the reason spelled out. Receipt
  wording changed from `Matches (content hash)` / `Mismatch (content hash)`
  to `Checksum matches` / `Checksum mismatch (record edited after it was
  written)`, with a note that the unkeyed checksum detects edits and does
  not prove authorship. `ShardReceipt` gains `integrity_status`
  (`valid` / `mismatch` / `missing`) and `post_session_verification`.
- `trust last` and `proof last` now locate history the same way as `last`
  (nearest `.openshard/runs.jsonl` from any subdirectory) instead of the
  current directory only.
- **First run.** The Home screen no longer says `Mode: Configured` /
  `Model: Claude Sonnet 4.6` in a repository with no OpenShard config: the
  bundled defaults are not the person's configuration, so it says
  `Not configured` and agrees with `doctor` and `setup --agent`.
- `NO_COLOR` is no longer treated as an agent environment anywhere
  (`is_agent_environment`, `openshard env`, `output_mode` inference). It is
  a colour preference; a person who sets it gets plain human output, not
  agent JSON. Onboarding already ignored it.
- Capture-profile `import_note` text no longer says verification is "never
  recorded" (verification v2 records agent-reported outcomes); new records
  say the outcome is the agent's own report until `openshard verify` re-runs it.
- `openshard demo shard` copy: "Trust is a heuristic over the recorded proof
  signals, not a safety guarantee" (was "whether the run is safe to rely on"),
  and its verification line names the source.
- `~/.openshard/claude-capture.json` and `~/.openshard/telemetry.json` are
  written owner-only (0600) on POSIX.
- **Google Antigravity 2.0:** re-audited against the unified hooks reference.
  - A non-empty `error` stays a reported failure.
  - An empty `error` on `run_command` is not treated as a pass, because
    commands can continue in the background after `WaitMsBeforeAsync`.
  - A `PostToolUse` with no `toolCall.name` (pre-1.1.9 non-tool steps) is now
    ignored.
- **Hermes Agent:** `status: cancelled` is now a failed tool call. A blocked or
  cancelled check command has no result (`unknown`).
- Command classification is stricter for dangerous Git, Terraform and
  Kubernetes commands and for executable aliases on Windows. Callers that
  pass explicit approval must now record where it came from.

### Fixed

- **OSN verifier timeouts stay unknown.** A verification command that starts but times out no longer becomes a failed model outcome or triggers Supervisor/model recovery. The run stops with `verifier_timeout`, the Receipt records unknown directly observed verification with incomplete evidence, and legacy `verification_passed` stays unset.
- `proof last` coerced the record with the write-path default and so stamped a
  fresh content hash on a historical Receipt that never stored one; the proof
  contract now reads the record as-is, so integrity stays "Not recorded" and
  reading never manufactures integrity evidence.
- `openshard setup` printed the Antigravity row twice.
- Older native records that stored `verification_passed` without
  `verification_attempted` read as "not recorded"; an outcome now implies an
  attempt.
- `openshard models sync-openrouter` fetched from `api.openrouter.ai`,
  which does not resolve; it now uses `openrouter.ai/api/v1/models`.
- Scored model selection could silently promote a brand-new model from the
  provider inventory (the shortlist keeps the highest version per family,
  so an uncurated `deepseek-v4.1-flash` would displace curated DeepSeek
  models). Scored routing now only considers curated or explicitly
  selected models.

## 0.4.7 - 2026-09-23

Four more capture surfaces: Google Antigravity, Hermes Agent and Grok Build
through the same authenticated local capture path as the other agents, and
Grok Bot (Cursor) through Cursor's OpenTelemetry export or a self-report
skill, each with its evidence level stated. Receipts gain a structured
`verification` block and a concise `task_title`, and sync now closes idle
sessions before sending them. Stored records are never rewritten.

### Added

- **Grok Build support**, through Grok's own native hooks (not its Claude
  compatibility layer) and the same capture path as the other agents,
  verified against a real Grok Build 1.0.41.
  `openshard capture install grok-build` (also run by `openshard setup` when
  `grok` is on PATH) writes OpenShard's own `.grok/hooks/openshard.json`
  (no other hook file is touched); `capture uninstall grok-build` and the
  `openshard hooks grok-build` entrypoint (fast path, authenticated loopback
  POST to `/hooks/grok-build`, always replies `{}`, never denies). Sessions
  become Shards labelled "Grok Build (external)". Subscribed events:
  `SessionStart`, `UserPromptSubmit` (the task), `PostToolUse`,
  `PostToolUseFailure`, `PermissionDenied`, `Stop`, `StopFailure`,
  `StopCancelled`, `SessionEnd`. Grok's payloads name no model, provider,
  tokens or cost, so those stay Not recorded; `PostToolUse` fires for every
  tool that ran (even a non-zero exit), so file tools stay `unknown`, a check
  is "attempted, outcome not observed", and git supplies the file evidence.
  The extra `Stop` Grok fires after `SessionEnd` and every subagent session
  (own session id, `subagentType`) are ignored. `PreToolUse` is never
  installed (no policy enforcement). `openshard doctor` reports a Grok Build
  install as unverified until a real session is captured and names the
  folder-trust step. See `docs/agent-capture.md`.
- **A Grok Build session is never recorded as Claude Code.** Grok also loads
  Claude Code hooks (e.g. a user-level `~/.claude/settings.json`) and sends
  them a document that is a valid Claude payload; the Claude receiver now
  refuses documents that carry Grok's own keys (`hookEventName`, `sessionId`,
  `workspaceRoot`), which previously produced a duplicate Claude-labelled
  Shard next to the Grok one.
- One agent-neutral addition to the shared fold: a `PermissionDenied`
  lifecycle event, recorded as an `agent_reported` `approval.denied` Event
  naming the tool only (`capture.permission_denied_count`); it is never work
  and never opens a Shard.
- **Hermes Agent support** (Nous Research), observation only, through the same
  authenticated capture path as the other agents. `openshard capture install
  hermes` adds `openshard hooks hermes` to the `hooks:` section of Hermes'
  user-global `config.yaml` (other hooks, `hooks.outbound` and comments
  preserved when there is no existing `hooks:` block; otherwise merged after a
  one-time backup) and records Hermes' documented first-use consent in
  `shell-hooks-allowlist.json`; `openshard capture uninstall hermes` removes
  only OpenShard's entries. Subscribed Hermes hooks: `on_session_start`,
  `pre_llm_call` (the task; replies `{}`, never injects context),
  `post_tool_call` (tool, arguments, Hermes' own `ok` / `error` / `blocked`
  status, duration, correlation ids), `post_api_request` (Hermes' per-request
  token counts, model and provider; no cost is reported, so none is
  recorded), `on_session_end` (per turn: completed / interrupted / neutral),
  `on_session_finalize`, `subagent_start` / `subagent_stop` and
  `pre_approval_request` / `post_approval_response`. `pre_tool_call` (the hook
  that can block or rewrite a tool call) is never subscribed. Sessions become
  Shards labelled "Hermes Agent (external)" with `capture.subagents` /
  `capture.approvals` counts and agent-reported approval Events. Hermes' hooks
  are user-global, so a repository is captured only when it has an
  `.openshard/` directory (created by `capture install hermes` in it) and never
  the home directory; `openshard setup` detects Hermes but does not edit its
  global config. `openshard doctor` reports the config, Hermes' allowlist,
  `HERMES_SAFE_MODE` and the repository opt-in. See `docs/agent-capture.md`.
- Agent-neutral additions to the shared fold: `SubagentStart` / `SubagentStop`
  and `ApprovalRequest` / `ApprovalDecision` lifecycle events, a bounded scalar
  `attrs` carrier on the reduced payload (durations, correlation ids, subagent
  and approval facts), and `AgentProfile.opt_in_repo` for agents whose hooks are
  configured user-globally.
- **Grok Bot (Cursor) capture**, two paths with explicitly different
  evidence (`docs/grok-bot.md`). *Enterprise:* `openshard grok-bot ingest`
  (file/stdin) and `openshard grok-bot serve` (bearer-authenticated OTLP/HTTP
  receiver) read Cursor's OpenTelemetry Export of Grok Bot Action Recording
  (`cursor.surface=grok_bot`; protobuf or OTLP/JSON, gzip, no new
  dependency). One Cursor conversation becomes one Shard, idempotent on
  `cursor.event.id`. Shell commands, Cursor shell-policy denials, MCP tool
  calls, browser hosts and computer-use counts become `directly_observed`
  Events with `observer = cursor_action_recording`, and tokens come from
  `api_request`. Exit codes, file changes, task text, cost and the
  conversation end are not exported and stay Not recorded / Not observable.
  *Every plan:* `openshard grok-bot skill` prints a skill that has the Bot
  run `openshard grok-bot report` on the user's desktop (Execution on Local
  Computer). Those Shards are `agent_reported` throughout and always
  `incomplete` (`integration_limitation`). No MCP connector is shipped: Grok
  Bot cannot reach local MCP servers, and a public one would add no
  evidence.
- Receipts: the Evidence row names a
  third-party observer when every directly-observed event has the same one.
  "Changed" reads "Not observable" when an integration stores
  `changes.files_observable = false`. Existing records render unchanged.

- **Google Antigravity support**, through the same capture path as the
  other agents. `openshard setup` detects `agy` / `antigravity` and adds an
  `openshard` hook to the project-local `.agents/hooks.json` (other named
  hooks preserved); `openshard capture install|uninstall antigravity` and
  the `openshard hooks antigravity` entrypoint (fast path, authenticated
  loopback POST to `/hooks/antigravity`, background fold). Sessions become
  Shards labelled "Google Antigravity (external)". Subscribed events:
  `PreInvocation` (one per model call: activity and the model used),
  `PostToolUse` (commands, file writes with Antigravity's own success
  signal, file reads) and `Stop`. `PreToolUse` is never installed: it is a
  permission gate and OpenShard only observes. Every model a session used
  is kept (`capture.models_seen`, one `model invoked` Event per switch).
  Antigravity exposes no prompt, token counts, cost, provider or session
  end to hooks, so those stay Not recorded and idle sessions are closed by
  the sweep as `session_end_not_observed`. Workspace hooks load only in a
  folder that belongs to an Antigravity project (confirmed with Antigravity
  1.2.9). See `docs/agent-capture.md`.
- Two agent-neutral additions to the shared fold: a `ModelInvocation`
  lifecycle event and a `read` tool kind (a repo-relative path read, never
  a change or an attempted edit). The capture service anchors a session's
  change-attribution baseline at its first model invocation when the agent
  has no start hook.
- **Concise Receipt task titles.** Receipts carry a short `task_title`
  (<= 60 chars, <= 9 words, `history/task_title.py`) alongside the unchanged
  `task_short` / `task_full`, which keep the original task text. Capture
  writers stamp a deterministic title (never a model call on the capture
  path); older records derive one at read time. `task_title` appears in
  `openshard history --json` and in the sync envelope.

### Fixed

- A session without an end hook (Google Antigravity, or any session whose
  end was missed) was synced after an hour idle *before* capture closed it:
  the hosted copy claimed a complete capture, and the later idle sweep
  (which only ran when the agent's next session started) changed the local
  record to `session_end_not_observed`, leaving the hosted copy stale.
  Sync now runs the idle sweep first and waits while a session's capture
  buffer is still open.
- A hook that names the model without a provider (Hermes' session hooks) no
  longer downgrades an already-observed `provider/model` to the bare model slug.
- **Verification evidence no longer disappears before the Receipt.** The
  machine `verification_status` was filled only from native OSN runs, so
  every hook-captured, imported or wrapped Receipt crossed `history --json`
  and sync with `verification_status: null` -- shown by the Platform as "No
  verification recorded" even when a check command had been observed.
  Imports and wraps also stored `verification_attempted: false`, which read
  as "No checks run" although those paths cannot see checks. Receipts now
  carry a structured `verification` block (`history/verification.py`):
  `status` (`passed` / `failed` / `partial` / `not_run` / `unknown`),
  `source` (`agent_reported` / `directly_observed` / `git_verified` /
  `independently_verified`), `observation_mode`, check counts and names,
  timestamps, exit code, `artifact_sha`, and `complete` /
  `incomplete_reasons`. "Attempted, outcome unknown" is `unknown`, never
  `not_run`; malformed evidence becomes `unknown` and incomplete, never
  silently dropped. Hook capture records every check-shaped command it sees
  (bounded, surviving the event cap and buffer rebuilds). An invocation
  seen in a received hook event is `directly_observed` with status
  `unknown` and `outcome_not_observed`; only a failure resting on the
  agent's own "tool failed" signal is `agent_reported`. Import and wrap
  record `not_observable`.
- `build_live_run_receipt` no longer shows "No checks run" for a run whose
  verification was attempted without a recorded outcome.

### Compatibility

- Stored records are never rewritten. A record without a `verification`
  block gets one derived at read time (`derived: true`) from the fields it
  has; a record that recorded nothing about verification still projects
  `verification_status: null`. OSN tokens (`skipped`, `manual_review`) are
  unchanged. `verification_status` may now also be `partial`.
- The full block appears in `openshard history --json` (extended
  projection) and in the sync envelope, alongside the flat
  `verification_status` and a `verification_reason` that names the source
  (e.g. `"... [directly_observed]"`).

## 0.4.6 - 2026-09-18

OpenShard Platform sync arrives: `openshard sync connect` / `now` / `status`
send this repository's Receipts to a hosted history, retry-safe and
idempotent, off until a Platform link is configured. Receipt `task_id`
propagates through sync unchanged, and the extended receipt projection now
carries canonical `host/owner/repo` identity. No existing Receipt semantics,
on-disk format, or MCP output change.

### Added

- **`openshard sync`: hosted Receipt history.** `openshard sync connect`
  stores an OpenShard Platform link (endpoint, organisation, `osk_` API
  key) in `~/.openshard/platform.json` (mode 0600, never in a
  repository); `openshard sync now` sends this repository's Receipts as
  the receipt sync envelope v1 (the exact `openshard history --json`
  projection: no prompts, transcripts, diffs, output, notes or paths),
  keyed by `receipt_id`; `openshard sync status` shows what is synced,
  pending, still in progress, changed locally, in conflict or rejected.
  Sending is idempotent and retry-safe: pending work is derived from
  `runs.jsonl` against `.openshard/sync-outbox.jsonl`, a replay is a
  no-op, conflicts and rejections are recorded once and never retried,
  an unreachable Platform backs off exponentially, and an open agent
  session is left alone until it ends or has been idle for an hour. The
  capture service syncs every repository it knows on a timer once a link
  exists. `OPENSHARD_PLATFORM_SYNC=off` or `platform: {sync: false}` in a
  repository's config turns it off. See `docs/platform-sync.md`.
- **`task_id` syncs unchanged.** The receipt sync envelope carries the
  record's explicit `task_id` (v0.4.6) exactly as stored, `null` for
  Receipts that never declared one. Sync never mints or infers one.
- **`repo_identity` in the extended receipt projection.** `openshard
  history --json` now includes the record's canonical `host/owner/repo`
  beside the folder-name `repo` (which hook-captured records never
  carry). The MCP `get_receipt` key set is unchanged.

## 0.4.5 - 2026-09-16

OpenCode capture that loads where OpenCode actually runs, diagnostics that
say only what is proven, and Receipts whose integrity survives OpenShard's
own amendments. No new integrations, no new commands, no telemetry
broadening. Authenticated repo+agent scoped capture, fail-open agent
behaviour, `receipt_id` / `shard_id` semantics and the readability of every
existing on-disk record are unchanged.

Verified end to end on Windows with the real OpenCode Desktop application:
a Desktop session was captured (21 accepted OpenCode deliveries), recorded a
new Receipt with `Executor  OpenCode (external)` and `Integrity  Matches
(content hash)`, and `doctor` reported `✓ Capture verified`. The OpenCode CLI
(Bun) and a Node with TypeScript type stripping forced off are covered by
the test suite.

### Fixed

- **The OpenCode project plugin now ships as plain JavaScript
  (`.opencode/plugins/openshard.js`).** OpenCode's CLI runs on Bun, which
  strips TypeScript types natively, so the previous `openshard.ts` loaded
  there. OpenCode Desktop runs its server in an Electron utility process
  whose bundled Node is compiled without amaro; that Node refuses a `.ts`
  plugin with `ERR_UNKNOWN_FILE_EXTENSION`, OpenCode logs "failed to load
  plugin" and continues, and the session edits the repository while
  OpenShard captures nothing. Plain ESM JavaScript loads under both
  runtimes. Behaviour, bounded payloads, the repo+agent scoped capability
  and fail-open buffering are unchanged; only the extension and the
  (erasable) type annotations differ. Plugin payload version 4 -> 5. The
  node-harness tests now run with type stripping forced off, reproducing
  the Desktop runtime.
- **Authenticated OpenCode Desktop deliveries are no longer refused as
  browser traffic.** With the plugin loading in Desktop, every event it sent
  was still answered `403` before its capability was checked: the capture
  service refuses requests carrying browser-only headers, and its list
  included `Sec-Fetch-Mode`, which Node's undici `fetch` (the runtime under
  Electron) attaches as `Sec-Fetch-Mode: cors` to every request even though
  it is not a browser. Bun's fetch does not, which is why only Desktop was
  affected. `Sec-Fetch-Mode` is dropped from the browser-header set;
  `Origin`, `Referer` and `Sec-Fetch-Site` remain refused outright, with or
  without a token, because a cross-origin browser request always carries
  `Origin`. The repo+agent scoped capability stays the primary gate and
  authentication is not weakened; Claude Code, Codex and Cursor capture are
  unaffected.
- **`openshard setup` / `openshard capture install opencode` migrate an
  OpenShard-owned legacy `openshard.ts` safely.** Install and uninstall
  remove a pre-0.4.5 `openshard.ts` that carries the OpenShard marker, so a
  repository never loads both files (double capture under Bun; a repeated
  load error under Desktop's Node). A user's own `openshard.ts` without the
  marker is never touched. `doctor` / `setup` report a leftover OpenShard
  `.ts` as an outdated plugin and prompt a reinstall instead of reporting
  the integration absent.
- **`doctor` separates "configured" from "capture actually observed".** The
  OpenCode plugin runs inside OpenCode's own runtime, which can silently
  decline to load it (`opencode run --pure`, a Desktop build that cannot
  load the plugin, a stalled plugin-dependency wait, a stale plugin). In
  that state `doctor` previously showed `✓ Capture plugin`. It now adds a
  separate **Capture verified** check that is green only when an OpenCode
  session has actually been recorded in this repository's
  `.openshard/runs.jsonl`, and the summary reads `Configured but
  unverified` until then. `--json` gains `opencode.capture_observed` and
  `opencode.capture_verified`. Capture itself, the translator and the
  capability auth are unchanged.
- **`openshard note` / `openshard feedback` no longer break a Receipt's
  integrity.** Both commands amended the latest record without re-stamping
  `content_hash`, so a fresh Receipt went from `Integrity  Matches` to
  `Integrity  Mismatch` the moment OpenShard itself attached a note. They now
  go through one canonical amendment path (`history.store.amend_latest_record`)
  that verifies the stored hash first, applies the change, records an additive
  `amendments` entry (`kind`, `recorded_at`, `source`, `integrity_before`,
  `content_hash_restamped`) and preserves the integrity verdict: a valid hash
  is re-stamped over the amended content; a legacy record with no hash is
  never given one; a record that already read as mismatched keeps its stored
  hash and stays `Mismatch`. `receipt_id`, `shard_id` and historical content
  are untouched.
- **History amendment and read behaviour is now consistent.** Every CLI
  reader uses one loader (`history.store.load_history`): the last line that
  parses to a JSON object is the latest record, malformed lines are skipped
  on read and preserved byte-for-byte on write, and a legacy record is never
  given a `content_hash` on read (`Not recorded` is never reported as
  `Matches`). The amendment path targets the same record the loader reports
  as latest. `note` and `feedback` also resolve `.openshard/runs.jsonl` from
  any repository subdirectory, the same way `last` / `history` do, and the
  feedback interaction/memory side-records land next to that history instead
  of in the current directory.

### Added

- **`openshard capture status` exposes accepted delivery counts by agent.**
  The capture service counts *accepted* (authenticated and queued)
  deliveries per capture agent, exposed as `stats.by_agent` on `/health` and
  rendered as `by agent: opencode 12, claude_code 40`. Counted only after
  authorization succeeds, so it cannot be spoofed and never weakens auth.
  This is the live signal for "is this agent actually delivering", the
  complement of the persistent per-repository `doctor` check above. No
  secret, path, prompt or repository name is added to any output.

## 0.4.4 - 2026-09-15

Receipt integrity hardening. No new integrations, no new commands beyond
`openshard capture rotate-token`, no telemetry broadening. The goal: a
Receipt never claims more than its evidence supports.

### Changed

- **Changed files are attributed, not assumed.** The working tree is
  snapshotted when a session is first observed; at every fold each path in
  the git diff is `agent_reported` (the agent's own success signal),
  `git_observed` (repository changed, actor not established),
  `pre_existing` (already dirty before the session, unchanged since --
  excluded from counts) or `other_session` (reported by another live agent
  session -- excluded). Receipts read `Changed  2 files (1 agent-reported;
  1 git-observed, actor not established)` with separate exclusion rows;
  `files_detail[].attribution` and a `changes` block are added to records
  and `--json` output. `files_created/updated/deleted` on new records count
  only this session's changes.
- **The local capture service is authenticated.** Every `POST` needs the
  per-user capture token (`~/.openshard/capture-token`, created locally,
  0600) or a capability derived from it and scoped to one repository and
  one agent; requests without one are refused before parsing and counted,
  browser-originated requests are refused, and `/shutdown` accepts only
  the token. Claude Code HTTP hooks carry their capability in a header
  (written by `setup` into the git-excluded `.claude/settings.local.json`;
  a `SessionStart` of the upgraded OpenShard upgrades older hook entries
  itself); the OpenCode plugin carries its own capability; Codex, Cursor
  and the status line run `openshard`, which reads the token file. The
  master token appears in no agent configuration. Agents remain fail-open.
- **Corrupt queued evidence is never forgotten.** Undecodable capture-queue
  lines are quarantined (bounded) under
  `.openshard/claude_sessions/quarantine/`, counted (`corrupt_lines`), and
  the affected record becomes `capture.completeness.status = incomplete`
  with the reason; valid neighbouring events are still applied. Transient
  I/O errors keep the retry path. Receipts keep `Capture  partial` (depth)
  and add `Gaps  None known` / `Gaps  N queued events could not be decoded`.
- **Status wording**: a finished agent turn renders `Turn completed
  (unverified)` (was `Completed`); a session that ended without a turn
  `Session ended (no turn observed)`.
- **Risk** is shown as recorded; the display-time rule that raised a review
  task's missing/Low risk to High is removed.

### Added

- `receipt_id` (`rcpt_` + 32 hex) on every new record, minted at creation;
  shown on receipts and in `--json`/MCP output; accepted by `get_receipt`
  and history search. `shard_id` is unchanged.
- `Integrity` row (`Matches (content hash)` / `Mismatch (content hash)` /
  `Not recorded`) on compact and full receipts.
- `capture.completeness` (`complete` / `incomplete` / `unknown` with
  reasons) on hook records -- separate from the unchanged capture depth
  (`full` / `partial` / `unknown`); records written before loss tracking
  read `unknown`, never `complete`. JSON carries both as
  `capture_completeness = {depth, status, reasons, derived}`.
- `openshard capture rotate-token`; `doctor`/`setup` report hooks with a
  missing or stale credential; `capture status` shows refused and
  quarantined counts. Telemetry `capture.service` gains two bounded
  counters, `rejected` and `corrupt_lines`.
- `docs/architecture.md`, `docs/architecture/V044_RECEIPT_INTEGRITY_AUDIT.md`,
  `docs/architecture/POST_V044_CORE_CLEANUP.md`; rewritten `SECURITY.md`
  and `CONTRIBUTING.md`; trust-boundary, attribution and completeness
  sections in `docs/agent-capture.md`.

### Fixed

- On Windows, several processes opening a brand-new history lock file at
  once could fail with `PermissionError`: the first process seeded and
  locked the sidecar's first byte while a second process's buffered seed
  write landed in that mandatory-locked range. The seed is now written
  unbuffered and a failed seed (which only ever means another process
  already holds the lock) falls through to the normal wait.

### Compatibility

- Records written before 0.4.4 render unchanged: no `receipt_id` is
  back-filled, attribution rows appear only when the record carries them,
  completeness is derived from existing counters. JSON output is extended
  additively; `files_changed` may be lower on new records because
  pre-existing changes are no longer counted.
- Hooks installed by 0.4.3 or earlier are refused by the 0.4.4 service
  until upgraded (`openshard setup`, or automatically at the next Claude
  Code `SessionStart`); the in-process fallback keeps recording meanwhile.
- The test suite now isolates the capture service's default port per test
  and never touches a developer's real service.

## 0.4.3 - 2026-09-12

A DX cleanup pass around the external-agent receipt loop. No commands were
removed or renamed, and no behaviour changed beyond presentation.

### Changed

- `openshard --help` now groups commands into sections (Getting Started,
  Receipts, Diagnostics, Integrations, Advanced) instead of one flat
  alphabetical list. Every command still runs exactly as before; this only
  changes what `--help` shows.
- `openshard setup` is now the one command the README and grouped help point
  new users at. `openshard init` (a lower-level onboarding-preferences
  command) keeps working unchanged; its `--help` text now points to `setup`.
- Terminal receipt output (`openshard last --more/--full`, `openshard report`,
  etc.) reorders existing fields so files/changes and checks appear before
  the policy/approval block, matching the target receipt layout. No fields
  were added, removed, or renamed; `--json` output is unchanged.
- README now leads with the beginner flow (`pip install openshard` ->
  `openshard setup` -> use your coding agent -> `openshard last`); the full
  command-by-command reference moved to `docs/cli-reference.md`.

### Internal

- `hooks claude|codex|cursor|claude-status`, `capture serve`, and
  `shard verify last` are now hidden from `--help` (they are automation
  entrypoints and a documented alias, respectively). They remain fully
  callable and documented in `docs/cli-reference.md`.

## 0.4.2 - 2026-09-12

The clean recovery release that closes the v0.4.x external-agent receipt
chapter. It contains everything in 0.4.1, two intentional additions, and a
fix for the capture service's shutdown path.

**Note on PyPI 0.4.1.** The `openshard==0.4.1` package on PyPI was built from
a local working tree rather than from the `v0.4.1` git tag, and shipped an
early, unreviewed copy of the Cursor and telemetry work below. It has been
yanked. The `v0.4.1` GitHub tag and release are correct and remain valid.
Releases are now built and published only from the pushed tag by the
`release.yml` workflow (see `docs/release-checklist.md`).

### Included from 0.4.1

- All receipt-capture correctness fixes: nested repo-relative paths are no
  longer dropped from changed-file evidence ("Changed 0 files"), and a
  directly observed check command shows as "Attempted (unverified)" rather
  than "Not run". See the 0.4.1 entry below for details.

### Added

- **Cursor support**, alongside Claude Code, Codex and OpenCode. `openshard
  setup` detects Cursor and merges OpenShard's hooks into the project-local
  `.cursor/hooks.json` (unrelated hooks preserved; Cursor reloads the file
  without a restart). New `openshard capture install|uninstall cursor` and
  the `openshard hooks cursor` entrypoint; `doctor` and `setup` report
  Cursor's status like the other agents. Cursor sessions become Shards in
  the same `.openshard/runs.jsonl`, labelled "Cursor (external)", and appear
  in `openshard history`, `openshard context` and the MCP tools together
  with every other agent's work. Every hook is installed fail-open
  (`failClosed: false`), and the one blocking event OpenShard subscribes to
  (`beforeSubmitPrompt`) is always answered `{"continue": true}` whether or
  not capture succeeded: OpenShard observes Cursor, it never gates it.
  Evidence honesty is preserved: Cursor does not expose a tool success
  signal, cost or token counts, so a Cursor receipt records file tools as
  unknown, never claims verification, and shows cost as Not recorded.
- **Telemetry ("Help improve OpenShard"), added intentionally.** The
  telemetry preference is `unset` on every install and nothing is sent in
  that state. Basic privacy-safe telemetry becomes `on` once setup has run: a person sees the
  notice during `openshard setup` or the onboarding flow, and an agent
  running `openshard setup --json` gets the same notice back in the result
  (`telemetry.privacy_notice`, with `telemetry.agent_instruction` telling
  it to show the notice to its owner verbatim), so agent-driven setup can
  never hide it from the person. `openshard setup --agent` stays a
  read-only snapshot that never decides. `openshard telemetry off` turns it
  off immediately and discards the queue. What can be sent is a closed,
  versioned schema of usage and reliability data only: counts, durations,
  the OpenShard version, coarse OS/architecture/Python, fixed category
  values and a random pseudonymous per-install id. Prompts, source code,
  diffs, repository names, paths, file names, commands, model slugs,
  secrets, receipt contents and any other free text are excluded by
  construction -- the schema has no free-text field. `OPENSHARD_TELEMETRY=off`,
  `DO_NOT_TRACK=1`, a CI environment (`CI`, `GITHUB_ACTIONS`, `GITLAB_CI`),
  or `telemetry: {enabled: false}` in a repository's `.openshard/config.yml`
  also turn it off, and setup records no decision while one of those is
  set. Richer development data is a separate future level and stays off.
  `openshard telemetry status|on|off|reset|sample`; `sample` prints the
  exact queued events verbatim. The complete contract is in
  [docs/telemetry.md](docs/telemetry.md).

### Fixed

- The capture service could livelock on shutdown when a session's replay
  had a retry pending: the worker re-read its own stop sentinel in a loop
  that never fired the retry, spun until `stop()` gave up on the join, and
  stayed alive as a daemon thread. On Windows the trigger is the antivirus
  `PermissionError` that schedules such a retry; in the test suite the
  leaked thread stalled whole CI jobs (#323). The drain now fires due
  retries itself, waits in bounded slices instead of spinning, and ends at
  a deadline derived from `stop()`'s timeout -- a session that still cannot
  be replayed stays on disk for the next start to recover, as after a
  crash. `serve()` also restores the interpreter's switch interval on exit.

## 0.4.1 - 2026-09-08

Patch release: receipt-capture correctness fixes for Claude Code, Codex,
OpenCode, and `openshard wrap claude`. No new features, no schema or
positioning changes.

### Fixed

- Nested repo-relative file paths (e.g. `evals/basic/bug_fix/fixtures/
  word_utils.py`) were silently dropped from changed-file evidence,
  showing "Changed 0 files" in the receipt even when a tool call had
  genuinely edited the file. The generic secret-scrubbing heuristic used to
  sanitize paths treated an ordinarily nested path's `/`-joined segments as
  a "long opaque key-like run" and rejected it outright. Changed-file
  detection (both git-diff-inferred and agent-reported) now uses a
  dedicated path sanitizer that keeps the specific credential-shaped checks
  (API keys, bearer tokens, `password=...`) without misfiring on ordinary
  directory nesting. Fixes Claude Code hooks, Codex, OpenCode, and
  `openshard wrap claude`, which all shared the same flaw.
- A directly observed test or lint command (e.g. `pytest`, `ruff`) now
  shows as **"Attempted (unverified)"** in the receipt instead of **"Not
  run"**, when OpenShard saw the command execute but never read its
  output. Previously this case was indistinguishable from a session where
  no check ran at all.
- The distinction between externally observed work and OpenShard-verified
  execution is unchanged: OpenShard still never reads a Bash command's
  stdout or exit code for a Claude Code/Codex/OpenCode session, so
  `verification_passed` stays unset (`null`) for these captures regardless
  of what the command's output said. "Attempted (unverified)" reflects only
  that a check-shaped command was seen running — never a pass/fail result.

## 0.4.0 - 2026-09-05

### Added

- Modernized the local MCP server onto MCP SDK v2 (`mcp>=2.0,<3`, replacing
  `mcp>=1.2,<2`'s `FastMCP`/`ValueError` API with `MCPServer`/`ToolError`).
  No change to the server's tools, their arguments, or their output shape.
- PR13 effectiveness benchmark (`evals/pr13/`): a local, no-network harness
  that measures how much OpenShard's captured history actually helps an
  agent, across Claude Code, Codex, and OpenCode, with its own README and
  results summary. Not part of the shipped CLI.
- Codex and OpenCode capture: `openshard setup` now detects and configures
  Codex and OpenCode alongside Claude Code, in any mix, in the same
  repository. Codex hooks are merged into the project-local
  `.codex/hooks.json`; OpenCode gets a small plugin at
  `.opencode/plugins/openshard.ts`. All three feed the same local capture
  service and the same `.openshard/runs.jsonl`, so `openshard history`,
  `openshard context`, and `relevant_context` see every agent's work
  together, each Shard labelled with the agent that produced it. New
  `openshard capture install|uninstall codex|opencode` commands; `doctor`
  and `setup` report each agent's status independently. See
  [docs/agent-capture.md](docs/agent-capture.md).
- Evidence-backed recovery observations (PR11): when `relevant_context`
  ranks a Shard whose most recent verified attempt passed after an earlier
  verified failure, the match now includes a `recovery` observation —
  the failed attempt, the files changed and tools invoked in between, and
  the passing attempt. This is chronology, not causation: OpenShard never
  claims the intervening activity *caused* the pass, only that it was
  observed between the two verification results. No observation is added
  when the Shard's latest verified state is a failure, or when neither
  attempt has a real verification result.
- Improved `relevant_context` ranking: scoring and result assembly in
  `openshard/history/query.py` were reworked for better signal quality.
  Every rendering path (`openshard context`, the MCP tool, `--text`) still
  shares the exact same ranking. No embeddings, fuzzy matching, or model
  calls.
- Local stdio MCP server (`openshard.mcp.server`, `openshard mcp install
  claude`): a read-only server scoped to one repository's local history,
  exposing `recent_shards`, `get_shard`, `get_receipt`, `search_history`,
  and `relevant_context` as MCP tools. No network access, no API key.
- Local history query layer (`openshard/history/query.py`,
  `openshard/history/locate.py`, `openshard/history/views.py`): the shared
  read path behind `openshard history`, `openshard context`, `openshard
  stats`, and the MCP server's tools, all built on one privacy-bounded dict
  projection so the CLI and the MCP server can never diverge.
- Canonical Shard/Attempt/Event model: run history now has one typed
  `Event` model (`openshard/history/event.py`) instead of ad hoc dicts,
  used consistently by native OSN runs, the Claude Code import path, and
  receipts; and persistent Shard **attempt** grouping, so retries of the
  same task are tracked as attempts of one Shard rather than unrelated
  runs. This is the foundation the local history query layer,
  `relevant_context`, and the richer receipts below are built on. No
  change to `runs.jsonl`'s on-disk format for existing fields.
- Near-zero blocking Claude Code capture (PR9.5): the hooks Claude Code waits
  on no longer spawn a Python process or fold a receipt. `UserPromptSubmit`,
  `PostToolUse`, `PostToolUseFailure`, `Stop` and `SessionEnd` are installed
  as Claude Code's official **HTTP hooks**, POSTing the payload to a warm,
  loopback-only local capture service (`openshard capture serve`, started
  automatically by the `SessionStart` command hook and by `openshard setup`).
  The service's blocking path only validates, reduces the payload to the
  same privacy-safe shape as before (scrubbed task excerpt, repo-relative
  path, summarized command -- never raw prompts, transcripts or absolute
  paths), appends it to a per-session queue file with `fsync`, and returns;
  a background worker then replays the queue through the unchanged fold
  logic, so Shard records and receipts are byte-for-byte what the
  synchronous path produced, just eventually consistent (normally within a
  few hundred milliseconds). Replays are idempotent (every queued event has
  an id the session buffer remembers) and leftover queues are recovered on
  service start and on the next `SessionStart`, so a crash or kill loses no
  acknowledged event. The service exits by itself after 4 idle hours, on
  `openshard capture stop`, or on `mcp uninstall claude`; a port taken by
  another program is skipped for the next one in a small range and
  `setup`/`doctor` report and repair the hook URLs. `openshard capture
  status|start|stop|serve` are new; `doctor` gains a "Capture service" line;
  `setup` reports the service state. The command-form entrypoints
  (`openshard hooks claude`, `openshard hooks claude-status`) now forward to
  the service and fall back to in-process handling only when it cannot be
  reached (`OPENSHARD_CAPTURE_DISABLE=1` forces the old in-process path).
  Existing pre-PR9.5 hook configurations keep working and are upgraded in
  place on the next `openshard setup`. See `docs/capture-performance.md`.
- Strong local visibility (Free v0.4.0): a user never has to trust that
  OpenShard is "working in the background" -- four commands show exactly
  what it captured, offline, for the current repository:
  - `openshard last` is the polished "what just happened?" view: task,
    status, executor, model(s), duration, provider-reported tokens,
    estimated cost, changed files, checks, capture depth, evidence kinds and
    result, straight from the newest Shard receipt. When run from a
    subdirectory it says which repository root the history came from;
    `--json` gains a `repo` block (identity, folder name, relative history
    path -- never an absolute path).
  - `openshard history [--limit N] [--repo R] [--json]` lists recent Shards
    newest first: time, shard id, task, agent, the status OpenShard can
    truthfully claim (a completed Claude Code turn is shown as `Completed`,
    never as verified), a check summary, estimated cost, changed-file count,
    attempt count and partial-capture marker.
  - `openshard context "<task>" [--limit N] [--text] [--json]` exposes the
    same `relevant_context` the local MCP server gives an agent, with the
    signals that matched each Shard, its score, status/verification, retry
    history, changed files, non-Note findings and provenance (who ran it,
    how completely it was observed) -- plus a plain-English "How ranking
    works" footer generated from the scorer's own constants. `--text`
    prints the exact block an agent would receive. Retrieval quality is
    unchanged (PR10).
  - `openshard stats [--limit N] [--repo R] [--json]` gives honest counts
    derived from existing receipts: Shards/attempts/retries, agents, origin
    and capture depth, models (with an explicit `unknown` bucket),
    verification outcomes, Claude Code turn status (labelled as not being
    verification), estimated cost with the number of Shards it covers and
    the number missing it, provider-reported token totals, observed
    duration, files changed and the most-changed files. No productivity or
    efficiency scores. `stats completeness` / `stats failures` are unchanged.
  - `openshard.history.locate` resolves the history root for all of the
    above; `openshard.history.views` holds the single privacy-bounded dict
    projection shared by the MCP server and the CLI `--json` surfaces.

- Zero-friction onboarding: `openshard setup` is now the one command a new
  user needs. It detects the environment (git repository, Claude Code CLI,
  existing MCP/hook/status-line configuration, local history writability),
  configures Claude Code capture for the current repository by orchestrating
  the existing `mcp install claude` installers (MCP server, auto-capture
  hooks, status-line enrichment), and reports one of three honest
  outcomes: ready, ready with a limitation (e.g. a custom status line it
  will not replace, so model/cost/token data stays unavailable), or not
  ready with the exact next step. Safe to re-run; already-configured
  components are left byte-for-byte alone. `--yes` skips the interactive
  provider wizard, `--json` returns a machine-readable result, and
  `--agent` remains a read-only status snapshot that never writes. No API
  key, account, or network is required.
- `openshard doctor` now includes a Claude Code checklist (Repository,
  Local history, Claude Code, MCP, Auto-capture hooks, Receipt enrichment)
  with a ✓/✗ per line, the specific problem for each ✗, and a one-line
  verdict; `--json` gains a `claude_code` block. Read-only.
- `openshard mcp uninstall claude` — reverses `setup` / `mcp install
  claude`. Removes only OpenShard's own local-scope MCP entry, hook
  entries, and status line (a custom status line is never touched);
  unrelated Claude Code settings and all `.openshard/` history are left
  intact.
- Richer Claude Code receipts:
  - A task's receipt is now available as soon as its turn finishes (`Stop`
    fires) — it never has to wait for `SessionEnd`, which stays independent
    session metadata (`capture.task_status`, distinct from verification).
  - Model, cumulative session cost, and input/output/cache token counts are
    now captured from Claude Code's *status line* (`statusLine` setting) —
    the only official, local, no-network surface that reports them; no hook
    payload carries this data. `openshard mcp install claude` configures a
    status line automatically when the project has none of its own yet
    (`--no-statusline` to skip); a `openshard hooks claude-status` command is
    installed as its entrypoint. Model switches mid-session are preserved
    (never flattened to one model); cost is windowed to this Shard's session
    (baseline-subtracted, never a whole-session cumulative total dumped onto
    one receipt) and always labelled `est.` — never billing truth.
  - Task-boundary duration (first prompt → most recent completed turn, not
    whole-session time), a repo-relative `Files` list with change-type
    letters, per-tool `Activity` counts, and an `Evidence` summary line are
    now shown in the compact receipt. All are additive and gated on data
    being present, so existing receipts render unchanged.
  - Unknown model/cost/tokens still render as `Unknown` / `Not recorded` —
    never guessed from names, env vars, or user text.
- Claude Code auto capture: `openshard mcp install claude` now also installs
  OpenShard's Claude Code lifecycle hooks (`SessionStart`, `UserPromptSubmit`,
  `PostToolUse`, `PostToolUseFailure`, `Stop`, `SessionEnd`) into the
  repository's `.claude/settings.local.json`, so normal `claude` sessions are
  recorded automatically as Shards/Receipts with canonical Events — no
  `openshard import claude` / `openshard wrap claude` step. Pass `--no-hooks`
  to configure MCP only.
- `openshard hooks claude` — the non-interactive hook entrypoint Claude Code
  invokes (hook JSON on stdin, silent stdout, always exit 0).
- Untracked new files are now reported by the hook capture (`git ls-files
  --others`); work committed during a session is diffed against the HEAD
  snapshotted at session start.

### Fixed

- `openshard last` (and `stats completeness` / `stats failures`) read
  `.openshard/runs.jsonl` relative to the current directory, so running
  them from a subdirectory of a repository reported "No run history found"
  even though Claude Code hooks had recorded Shards at the repository root.
  They now resolve the repository's history root from any subdirectory
  (`repo`, `repo/subdir`, `repo/subdir/deeper` all read the same file),
  stop at the nearest `.git` so a nested repository never reads its
  parent's history, and never reach a sibling repository. Non-git
  directories keep the previous cwd behaviour.

### Changed

- Claude Code capture performance hardening: `Stop` (fires every turn) and
  `SessionEnd` are synchronous hooks, and the status line is inherently
  synchronous, so their latency was fully visible to the user. The
  `openshard` console script now fast-paths `hooks claude` /
  `hooks claude-status` around the full CLI's import graph (run pipeline,
  provider clients, evals, planning), and the status-line handler no longer
  performs a git diff / git-identity lookup / `runs.jsonl` rewrite on every
  ping — it only updates the lightweight staging buffer and lets the next
  real fold boundary (a throttled tool-hook snapshot, `Stop`, or
  `SessionEnd`) pick up model/cost/token values. Lock waits on the hook/
  status-line path are now bounded (fails open, Claude Code is never
  blocked) rather than unbounded. See `docs/capture-performance.md` for
  what was measured. No schema or behavior change to `runs.jsonl` or the
  hook command Claude Code invokes.

## 0.3.0 - 2026-06-06

First-class Claude Code session receipt import, skills list command, and tooling hardening.

### Added

- `openshard import claude` — import Claude Code session receipts directly into OpenShard history (PR #262)
- `openshard skills list` command to enumerate available skills (#257)

### Changed

- Import sorting and pyupgrade rules added to Ruff linting (#261)
- README revised for clarity on OpenShard's role

### Fixed

- `--from` flag renamed to `--notes` in `openshard import claude` for clearer UX
- Sandbox tests failing in environments with git commit signing (#259)
- Stale `claude-opus-4.6` reference in `config.yml` (#258)
- Added `pipx install` prerequisite to install docs and README (#260)

### Docs

- Updated `docs/what-is-a-shard.md`

## 0.2.0 - 2026-06-05

The proof, receipts, safety, and local history hardening release.

### Added

- Shard Proof Contract — a formal, consistent shape for run proof
- `openshard proof last` to inspect the latest run's proof
- Shard quality summary in `openshard last --json`, plus a compact
  `Proof: <status>` line in `openshard last`
- Content hash verification for Shards
- Run trust score, completeness stats, and failure taxonomy stats
- Generate an eval case from a failed Shard
- Best-effort pre-send secret scanning before provider calls
- CI check mode (pass / warn / fail / skip) with deterministic exit, plus
  GitHub Actions PR receipt outputs
- Machine-readable proof and run timeline output
- Repo map with caching and repo-aware plan mode
- First-run onboarding commands
- Model registry metadata and model lifecycle tags

### Changed

- Routing truth made clearer in proof output (routing behavior unchanged)
- License changed from MIT to Apache-2.0
- Runtime gate decision ordering unified across execution path

### Fixed

- Safer JSONL history writes (write locking)
- Model registry drift corrected to a single source of truth
- CI check GitHub Actions output test isolation
- Repo map path sanitisation on Linux

### Docs

- Added "What is a Shard?" explainer (`docs/what-is-a-shard.md`)

## 0.1.2 - 2026-06-01

- Published OpenShard 0.1.2 to PyPI
- Confirmed clean `pipx install openshard` path
- Fixed package config defaults for clean out-of-the-box install
- Improved package/source command parity
- Included proof-flow commands through the installed package