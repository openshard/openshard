# Guards

A guard is a check your team adds after correcting an agent's mistake. Once
it is committed, Openshard runs it on every later change that touches the
code it protects, whichever agent (or person) made the change. Fix a mistake
once; no agent repeats it.

Guards are plain files in your repository. They are reviewed and changed like
any other code.

## A guard file

One guard per file, in `.openshard/guards/<id>.yml`:

```yaml
id: billing-rounding
version: 1
status: active            # proposed | active | retired
kind: test                # test | note
title: Prorated seat charges round down
scope:
  paths: ["billing/**"]
check:
  command: "python -m pytest tests/guards/test_billing_rounding.py -q"
```

- `id` must match the file name (`billing-rounding.yml`).
- `scope.paths` are repository-relative globs: `*` within a folder, `**`
  across folders, and a trailing `/` for everything in a folder.
- `check.command` runs without a shell. Test-runner commands such as
  `python -m pytest ...` or `npm test` run unattended; anything Openshard does
  not recognise as a check needs approval (`--approve`), and dangerous
  commands never run.
- Only `active` guards of kind `test` run. A `note` is advice for agents and
  never runs or blocks.

## When guards run

- `openshard verify --commit <sha>` (and the automatic re-run after each
  agent commit) runs the guards whose scope covers what the commit changed,
  next to the commit's own checks. Each result is recorded with the
  verification as a check Openshard ran itself (`origin: guard`).
- `openshard guard run` runs the guards that cover your uncommitted changes,
  on the spot. It records nothing; use it while you work.
- `openshard guard list` shows the guards, and any file that is not a valid
  guard and why.

## Proving a guard works

A guard that passes on the code it was written against proves nothing. Before
turning one on, qualify it:

```
openshard guard qualify add-is-int --bad <agent's commit> --good <the fix> --activate
```

It runs the guard on an isolated copy of each commit, with the guard's own
test files copied in, then on your working tree:

| Result | Meaning |
| --- | --- |
| `qualified` | failed on `--bad`, passed on `--good`, passed every run on the working tree (`--runs`, default 3) |
| `passes_on_bad` | it does not catch the mistake |
| `cannot_load_on_bad` | pytest could not collect or import it on `--bad`: the code is new, which is not the same as catching the mistake |
| `fails_on_good` | it fails on the fix too |
| `flaky` | it failed at least once on the working tree |
| `could_not_run` | a run was blocked, needs `--approve`, timed out, or a copy could not be made |

The result is written into the guard file's `qualification` block; commit it
with the guard. `--activate` also sets `status: active`, only when qualified.

## Rules that keep guards honest

- **Guards are read as they were before the change.** The work being checked
  cannot weaken the rules it is checked against.
- **A change that edits or deletes a guard file, or edits a file a guard's
  command runs, does not get that guard run unattended.** The guard is
  planned as `needs_approval`, so the verification is not complete until a
  person runs it with `openshard verify --commit <sha> --approve`. Removing a
  guard is never silent, even when the change is outside the guard's scope.
- **A guard's verdict is the exit code Openshard observed,** never what an
  agent reported.
- **If guards exist but cannot be planned** (for example git cannot list the
  change), the verification is recorded as incomplete, not as passed without
  them.

## Committing guards

`openshard setup` keeps `.openshard/guards/` committable. If your
`.gitignore` ignores the whole `.openshard/` folder, guards would never be
committed; `openshard guard list` says so. Use these lines instead:

```
.openshard/*
!.openshard/config.yml
!.openshard/guards/
```

## Not yet

- `openshard guard suggest` and `guard draft`: finding corrections and
  drafting guards from them.
- Per-guard timeouts (`check.timeout_s` is read and stored but not enforced
  yet; the run's `--timeout` applies).
