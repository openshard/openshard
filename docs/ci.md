# CI: Fast PR Gate and Full Main Validation

OpenShard's CI is tiered. Pull requests get the fastest run that still
proves enough to merge safely; `main` and releases get everything.

| Tier | Workflow | Runs on | What it proves |
|------|----------|---------|----------------|
| 1 | **Fast PR Gate** (`ci.yml`) | every PR | ruff + mypy, the invariant suite, every test that can reach the change, Windows smoke slice when relevant |
| 2 | **Fast PR Gate**, full plan | PRs touching evidence-critical or CI paths | the whole PR suite: all parallel-safe files sharded, all serial capture-service files, Windows smoke |
| 3 | **Full Main Validation** (`main.yml`) | push to `main` | the complete suite, serial and unmodified, on Ubuntu and Windows x Python 3.11 and 3.12 |
| 4 | **Release** (`release.yml`) | `v*` tag | the same exhaustive matrix again from the tagged commit, then build, twine check, publish |

The one check to require in branch protection is the job named
**Fast PR Gate**. Its summary tab shows which jobs ran, which were
skipped by the plan, and why.

## How the PR gate decides what to run

`scripts/ci/select_tests.py` runs first, on the runner's stock `python3`
(it is standard-library only, so no install sits on the critical path).
It diffs `base...head`, classifies every changed path, and emits a plan:

1. **Docs-only** (`*.md`, `docs/**`, LICENSE, images, dotfiles): nothing
   runs except the plan and the gate.
2. **Full**: any change under an evidence-critical prefix or to CI
   infrastructure runs the whole PR suite no matter how small the diff.
   Evidence-critical prefixes: `openshard/history/` (Receipt/Shard schema,
   hash, persistence), `openshard/adapters/` (all capture adapters, hooks,
   capture auth), `openshard/verification/`, `openshard/security/`,
   `openshard/safety/`, `openshard/sync/`, `openshard/telemetry/`,
   `openshard/mcp/`, `openshard/util/`, `openshard/native/sandbox_*`.
   CI infrastructure: `.github/`, `scripts/ci/`, `pyproject.toml`,
   `tests/conftest.py`, `tests/capture_fixtures.py`.
3. **Targeted** (everything else): the **invariant suite** always runs,
   plus every test module that transitively imports a changed module.
   The dependency graph comes from real `import` statements (including
   ones inside function bodies) and from dotted `"openshard.x.y"` string
   literals, so `patch("openshard.run.pipeline.foo")` counts. Test files
   that reference no repository module are always included. If more than
   60% of the parallel-safe files are affected anyway, the plan escalates
   to full.

Anything the classifier cannot map (package data such as `*.yml` or
`*.tcss` inside the package, a deleted module, an unknown top-level file,
a git diff that fails) runs the full suite. Unknown means "run
everything", never "run nothing".

The invariant suite is the floor for every non-docs PR: Shard/Receipt
schema, hash, identity and proof contracts, v0.4.4 receipt identity and
semantics, provenance, evidence filtering, verification contracts,
history amendment integrity, the JSONL store, path/secret/shell safety,
platform sync. The list lives in `INVARIANT_TEST_FILES` in the selector;
`python scripts/ci/select_tests.py check` (run in every lint job) fails if
any listed file stops existing, so a rename can never silently drop
coverage.

To see what a change would run and why:

```bash
python scripts/ci/select_tests.py explain openshard/tui/app.py
python scripts/ci/select_tests.py plan --base origin/main --head HEAD
```

## Job layout

```
Plan ─┬─ Lint + types                             ruff, mypy, selector self-check
      ├─ Tests (shard 1/N .. N/N)                 parallel-safe files, pytest-xdist
      ├─ Tests (capture services, serial 1/2, 2/2) real HTTP capture service; serial within each job, never Windows
      └─ Tests (Windows smoke)                    curated platform-sensitive slice
                                  └──────── Fast PR Gate                    the required check
```

* **Shards.** The parallel-safe test files are split into up to three
  balanced shards by weighted file size (about 1.5 MB of test source per
  shard), so a full plan gets three, a typical targeted plan two, and a
  small one runs as a single job. Each shard runs `pytest -n auto`. A few
  files dominated by real subprocesses (the pr13 benchmark harness, the
  first-run UX tests) carry an explicit weight in `SHARD_WEIGHTS`; every
  shard prints `--durations=15` so the weights can be tuned from data.
* **Capture-service tests** (`SERVICE_TEST_FILES`) spin up a real
  background capture service with threads, sockets and in one case a Node
  subprocess. Contention between many of them at once on one machine is
  where the observed Windows flakes came from. Each job runs its files
  strictly serially in a single pytest process; the nine files are spread
  over two such jobs, and separate runners are separate VMs, so nothing is
  shared between them. They are never mixed into a shard and never run on
  Windows in the PR gate. They still run, unrestricted and serial, on
  every OS x Python combination on main.
* **Windows smoke** (`WINDOWS_SMOKE_TEST_FILES`) covers the code paths
  that actually differ by platform: `util/home.py` and `util/git.py`,
  the JSONL store's atomic replace, hook path/CRLF handling, sandbox
  diff/apply, repo-map git calls, CLI entrypoints. It runs when the plan
  says the change can reach one of those files.
* **Dependencies** are installed with `uv` (`astral-sh/setup-uv` with its
  cache) into the interpreter from `actions/setup-python`.
* A new push to a PR cancels the previous run; pushes to `main` never
  cancel each other.

## Expected timings

Measured baseline (September 2026, runs 35922708973 through 36003345568):

| Run | Wall clock | Critical path |
|-----|-----------|---------------|
| docs-only PR | ~9 s | `changes` job |
| any code PR | 3:20 to 3:30 | one job: install 8 to 13 s, xdist subset 76 to 123 s, then serial service tests 47 to 55 s |
| push to main | ~17 min | full serial suite on Windows py3.12 (11 to 16 min); Ubuntu 4 to 5 min |

Lint + type check took 33 to 39 s and the Windows smoke job 90 s, both
off the critical path.

Measured on this change's own PR (run 36014470426, full tier, before the
service split and shard weights): 1:43 wall clock. Plan 8 s; Lint 28 s;
shards 44 / 67 / 74 s (xdist steps 33 / 57 / 60 s); capture services 84 s
in one job (69 s of tests); Windows smoke 76 s (uv install 13 s versus 30
to 42 s with pip; tests 43 s). Ubuntu installs dropped from 8 to 13 s to
1 to 3 s with the uv cache.

Expected per scenario (the full-tier row is measured, the others are
derived from it; each job prints `--durations=15` to refine them):

| Change | Tier | Jobs | Expected wall clock |
|--------|------|------|---------------------|
| docs-only (README, docs/**) | docs | Plan, Gate | 10 to 15 s |
| small isolated Python change (one test file, a leaf module) | targeted, 1 shard | Plan, Lint, 1 shard (invariants + affected), Gate | 45 to 60 s |
| normal Core feature (routing, TUI, scoring ...) | targeted, 2 shards, or full | Plan, Lint, 2 to 3 shards, 2 capture-service jobs, Windows smoke, Gate | 75 to 95 s |
| capture / verification / evidence-sensitive change | full | Plan, Lint, 3 shards, 2 capture-service jobs, Windows smoke, Gate | 85 to 100 s (measured 1:43 with one service job) |
| merge to main | Full Main Validation | Lint, 4 full serial matrix jobs | unchanged, 12 to 17 min, dominated by Windows |
| release | Release | verify, 4 matrix jobs, build, publish | unchanged |

Why a "normal Core" change often runs most of the suite: `openshard/cli/main.py`
imports nearly every module, and about 145 test modules import the CLI, so
the import graph honestly reports most tests as reachable from most product
modules. The gate is still fast because that work is sharded and the serial
service tests run alongside instead of after. Making selection sharper
means breaking up the CLI module's import hub, which is product work, not
CI work.

## What did not change

* Every test still runs before code ships: on every OS x Python
  combination after merge, and again before a release.
* The capture-service tests keep the serialisation they need; nothing
  about their execution order or isolation changed, they only moved to
  their own job.
* `release.yml` is untouched.
