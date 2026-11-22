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
- `openshard guard qualify`: proving a guard fails on the agent's commit and
  passes on the fix before it becomes `active`. Until then, a person sets
  `status: active` after checking that.
- Per-guard timeouts (`check.timeout_s` is read and stored but not enforced
  yet; the run's `--timeout` applies).
