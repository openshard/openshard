# PR13 Phase 1 — real-agent OpenShard effectiveness benchmark

One question, one controlled experiment:

> Same repository state, prompt, model and tool surface. Control receives
> empty OpenShard history; treatment receives real OpenShard execution
> evidence. Does the verified engineering outcome change?

Both arms see one MCP server named `openshard` with the same five
read-only tools, descriptions and schemas. Control's is a benchmark-local
**placebo** ([placebo_mcp.py](placebo_mcp.py)) that answers as an empty
history would and never reads any history; treatment's is the production
`openshard mcp serve` over the preserved burn-in history. The two servers
are probed over MCP before the arms run and must fingerprint identically.

This directory is a benchmark, not a product feature. Nothing in it is
imported by the `openshard` package; it imports only public OpenShard APIs
(the Claude Code hook installer, the capture-service client, the history
query layer, and `build_server_argv` for the MCP server command line).

## Layout

```text
evals/pr13/
  run_benchmark.py                 CLI entry point
  placebo_mcp.py                   control arm's placebo OpenShard MCP server (standalone, no openshard import)
  benchmark/
    mcp_probe.py                   MCP-level probe: tool names/descriptions/schemas -> fingerprint
    config.py                      metadata.json -> validated ScenarioConfig
    workspace.py                   seed/clone at the exact commit, isolation, reset keeping .openshard/
    harness.py                     real `claude -p` execution + stream-json parsing
    capture.py                     burn-in capture via production hooks + private capture service
    verify.py                      benchmark-owned verification + known-failed-approach criterion
    results.py                     RunResult JSON, comparison.json, comparison.md
    runner.py                      preflight -> source -> burn-in -> reset -> arms -> comparison
  scenarios/1_previously_failed_approach/
    metadata.json                  exact source/commit, prompts, verification, failed approach, evidence
    burn_in_prompt.txt / evaluation_prompt.txt
    seed/                          the target repository ("relay"), built to a pinned commit id
    verification/hidden_tests/     benchmark-owned tests; never enter the workspace
  results/                         output (git-ignored)
tests/test_pr13_benchmark.py       deterministic tests (fake agent, no model calls)
tests/pr13_fake_claude.py          the fake Claude Code CLI those tests use
```

## Running it

```text
python -m evals.pr13.run_benchmark --scenario 1_previously_failed_approach --model <model id or alias>
```

`--model` is required and recorded verbatim; the model Claude actually used
is read back from its transcript (`system/init` and every assistant
message) and recorded separately. Useful options: `--repeats N` (A/B pairs
from one burn-in), `--arm-order BA`, `--max-turns`, `--timeout`,
`--max-budget-usd`, `--claude-bin`, `--run-id`, `--out`.

Requirements checked in preflight (any failure aborts with a coded error):
`git`; the Claude Code CLI; `openshard.mcp.server` importable in the
benchmark interpreter (the `mcp` extra); an `openshard` console script next
to that interpreter (it is what the SessionStart hook and the MCP server
run as).

## What a run does

1. **Source.** A `seed` scenario is turned into a git repository with a fixed
   author, committer, date and message (text LF-normalised, mode 100644),
   so its commit id is reproducible; it must equal the pinned
   `base_commit` or the run aborts (`seed_commit_mismatch`). A `git`
   scenario is cloned and the pinned commit must exist
   (`commit_unavailable`). Nothing is ever substituted.
2. **Burn-in (treatment workspace B1).** Fresh clone at the base commit.
   OpenShard's *production* hook installer writes
   `.claude/settings.local.json`; a benchmark-private capture service is
   started (`OPENSHARD_HOME` and `OPENSHARD_CAPTURE_PORT` set only in the
   agent's environment). The real Claude Code CLI runs the burn-in prompt
   non-interactively with no MCP servers. The runner then waits for the
   session's Shard to be folded into `B1/.openshard/runs.jsonl` by
   OpenShard's own capture path — the benchmark writes no history.
3. **Establish the failure.** Benchmark-owned verification runs from
   outside the workspace. The scenario's `known_failed_approach` criteria
   are evaluated from the verification outcome plus git's view of the
   changed files. `expected_evidence` is checked against the preserved
   history through `openshard.history.query`, including whether
   `relevant_context(<evaluation prompt>)` actually returns the burn-in
   Shard. If the attempt passed, failed differently, or left no usable
   evidence, the run aborts with `burn_in_did_not_fail`,
   `burn_in_failed_differently` or `burn_in_evidence_missing`.
4. **Reset code, keep history.** Hooks are uninstalled; `git reset --hard
   <base>` then `git clean -fdx -e .openshard`; the runner proves HEAD is
   the base commit, `runs.jsonl` is byte-identical (sha256 before/after),
   and nothing but `.openshard/` survives. A copy of `.openshard/` is kept
   under `burn_in/history_snapshot/`.
5. **Arms.** Control A is a fresh clone at the base commit with no
   `.openshard/`. Treatment B is the burn-in workspace (for repeats > 1,
   fresh clones that receive a byte-for-byte copy of the snapshot). Both
   run the same Claude binary, model, flags, prompt and scrubbed
   environment, in the configured order. Both `--mcp-config` files declare
   one server named `openshard`: A's launches `placebo_mcp.py` with the
   benchmark interpreter; B's launches `openshard mcp serve --repo-path
   <B>` (the production command from `build_server_argv`). Before either
   arm runs, both servers are started over MCP by the benchmark itself,
   their tool lists compared to the production tool set and to each other
   (`mcp_surface_mismatch` aborts the run), and the fingerprint recorded.
6. **Verify and report.** Verification, the repeated-failure criterion,
   Claude's own accounting, and every isolation fact are written to
   `arm_A_1/run.json`, `arm_B_1/run.json`, `comparison.json`,
   `comparison.md` and `benchmark.json`.

## A/B isolation, exactly

| Concern | Mechanism |
|---|---|
| Same code | Independent clones (`--no-hardlinks`) at the pinned commit; HEAD re-checked before every run; `git status --ignored` must be empty apart from `.openshard/` |
| No nesting / sharing | `assert_isolated` (distinct, non-nested directories) |
| No treatment data in control | A never has `.openshard/`; asserted before and after the run; A's placebo server reads no files at all |
| Same tool surface | `--strict-mcp-config --mcp-config <arm file>` — the machine's own MCP servers are invisible to both arms; both declare one `openshard` server with the same five tools; A: placebo (empty answers), B: production over B's `.openshard/`; probed and fingerprint-compared before the run; Claude's init event must list the same `mcp__openshard__*` names in both arms |
| Machine settings | `--setting-sources project,local` (user-level model/permission/hook settings not loaded), `--disable-slash-commands`, `--no-session-persistence` |
| Environment | inherited `CLAUDECODE*`, `CLAUDE_CODE_*` (except the OAuth token), `CLAUDE_PROJECT_DIR`, `CLAUDE_SESSION_ID` and every `OPENSHARD_*` variable removed; the names removed are recorded; values never persisted |
| Tools/interpreter | the benchmark interpreter's `Scripts`/`bin` directory is prepended to `PATH` for every arm, so `python` and `openshard` resolve identically |
| No hooks at evaluation | `.claude/` must not exist in either arm; `OPENSHARD_CAPTURE_DISABLE=1` is set for both evaluation arms as a belt-and-braces guard |
| Post-run checks | both arms must report `openshard: connected` and no other server; control must still have no `.openshard/`; treatment's `runs.jsonl` must hash-match the burn-in snapshot; violations are recorded as validity errors (`completed_with_validity_errors`) |
| Provenance | every run records `openshard.mcp_server_kind` (`placebo` or `production`), the server command, and the probed surface fingerprint |

## Result schema (per run, `run.json`)

`scenario`, `arm`, `repeat`, `base_commit`, `workspace`, `run_dir`,
`agent{harness, claude_argv, model, max_turns, timeout_seconds, ...}`,
`model{requested, reported_by_claude_init, observed_in_assistant_messages}`,
`timing{started_at, ended_at, wall_clock_seconds}`,
`agent_exit{status, exit_code, timed_out, result_subtype, agent_reported_completion, num_turns, final_text}`,
`activity{tool_calls_total, tool_calls_by_name, bash_commands, files_changed, trustworthy}`,
`verification{passed, steps[...], failed_steps}`, `verified_success`,
`repeated_known_failure{matched, criteria[...], changed_paths}`,
`openshard{history_present, history_source, history{shards[...]}, mcp_configured, mcp_server_kind: placebo|production, mcp_config, mcp_surface{tool_names, fingerprint, matches_expected, init_event_tools_match}, mcp_servers_reported, mcp_server_status, retrieval_observed: yes|no|unknown, tools_called, tool_calls[...], burn_in_shard_surfaced_in_tool_results}`,
`usage{total_cost_usd, cost_provenance, tokens, tokens_provenance, model_usage, trustworthy}`,
`artifacts{...paths}`, `errors[]`, `notes[]`.

Unknown stays unknown: every value is either observed or `null`.
Cost/tokens come only from Claude Code's own `result` event and are
labelled as such. Assistant `thinking` blocks are never read; the raw
`agent_stdout.jsonl` is kept verbatim for audit only.

## Validity notes (read before interpreting a result)

* **One paired run proves nothing.** Model sampling is not deterministic.
  Use `--repeats` and report counts, not a verdict.
* **Hook capture records no verification outcome.** The treatment agent
  sees a prior Shard's task text, changed files, tool-call counts and
  `Status: Not recorded`; it does not see a failure verdict. That is what
  OpenShard's Claude Code capture actually provides today, and this
  benchmark measures that — not a hypothetical richer receipt.
* **Burn-in is a precondition, not a measurement.** The benchmark aborts
  unless the burn-in attempt failed through the scenario's known failed
  approach; re-running until it does is a selection on the precondition
  and is recorded (each run has its own directory).
* **The burn-in prompt names a file** (Scenario 1: `relay/_schema.py`) to
  make the failed approach a natural outcome; the evaluation prompt, which
  both arms receive, is neutral and names nothing.
* **MCP tools may be deferred.** In the Claude Code build this was smoke-
  tested with (2.1.259) MCP tools are loaded on demand: the treatment
  agent discovered `mcp__openshard__relevant_context` through `ToolSearch`
  before calling it. Whether an agent bothers is part of what is measured.
* **Arm order** is fixed per run (`--arm-order`) and recorded; alternate it
  across repeats if drift matters.
* **Environment symmetry has limits.** Both arms share the machine, the
  network, the Claude account and its rate limits.

## Scenarios

| # | Directory | Burn-in mechanism | Tests |
|---|---|---|---|
| 1 | `1_previously_failed_approach` | `claude_hooks` | exact-repeat known-bad approach; strong-model ceiling (Pilot 0: 3/3 vs 3/3, null result) |
| 2 | `2_multi_attempt_chronology` | `claude_wrap_chain` (new) | a real, `--shard`-linked multi-attempt Shard (failed attempt, then a corrected one) -- not PR11 `RecoveryObservation`, see the scenario's README |
| 3 | `3_prior_engineering_constraint` | `claude_hooks` | a non-obvious constraint learned on a *different*, related task |
| 4 | `4_stale_historical_evidence` | `claude_hooks` | harm test: same-domain but wrong-field prior evidence |
| 5 | `5_irrelevant_nearby_history` | `claude_hooks` | harm test: the weak end of relevance scoring (score 2, project name only) |
| 6 | `6_genuinely_novel_no_history` | `claude_hooks` | harm test: genuinely zero matches (docs-only burn-in, novel eval task) |
| 7 | `7_cross_agent_handoff` | `opencode_hooks` (new) | **inconclusive / not completed** — three live attempts never met the burn-in precondition (invalid model id; 402 insufficient credits; burn-in passed); no A/B result recorded. See `SUMMARY.md` |

**Final results for the six completed scenarios are in [`SUMMARY.md`](SUMMARY.md).**

Scenarios 2–7 (except 2 and 7) use the exact same scenario-agnostic
`claude_hooks` mechanism as Scenario 1 — only a new scenario directory
(source + commit, prompts, verification, criterion, expected evidence)
was needed, zero harness changes. Scenarios 2 and 7 needed real,
additive harness extensions (see `benchmark/config.py`'s `capture`/
`wrap_stages`/`agent` fields on `StagePolicy`, `benchmark/runner.py`'s
burn-in dispatch, and the new `benchmark/cross_agent.py`); Scenario 1's
behaviour and every other scenario's is provably unaffected — the same
64-test suite that covers Scenario 1 alone still passes unchanged.
Scenarios 2–6 share one target repository (`scenarios/_shared/
relay_seed/`, byte-identical to Scenario 1's own `seed/`, built to the
same pinned commit) rather than duplicating it six times; Scenario 1's
own `seed/` is untouched.

## Tests

`python -m pytest tests/test_pr13_benchmark.py tests/
test_pr13_scenarios_2to7.py` — 71 tests total, no model calls, no live
OpenCode session. The first file (38 tests) covers Scenario 1, the
placebo MCP server, and every scenario-agnostic harness module
end to end via a fake `claude` (`tests/pr13_fake_claude.py`) that
delivers real Claude Code hook payloads to OpenShard's production
adapter, so captured history is produced by the real capture path, never
fabricated. The second file (33 tests) covers scenarios 2–7: config
validation for the new `capture`/`wrap_stages`/`agent` fields, a full
Scenario 2 run through the real `openshard wrap claude` command (proving
genuine `--shard`-linked multi-attempt capture), a full Scenario 7 run
through a fake `opencode` (`tests/pr13_fake_opencode.py`) that calls
OpenShard's real OpenCode-plugin translator/fold directly, relevance-
scoring assertions for scenarios 5/6 verified against real history
entries, and hidden-test pass/fail checks for scenarios 3/4 against
hand-built buggy/correct implementations.

## Phase 3+

Open design choices for later phases: a scripted developer follow-up
session after a failed CI (the natural way failure knowledge enters
hook-captured history today), a Codex burn-in arm (not installed on the
machine this was built on, so untested even with a fake), and a
`git`-kind scenario against a public repository.
