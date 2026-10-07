# OSN acceptance suite

A small set of representative coding tasks run through `openshard osn run` on
a fixture repository, with one table at the end: what happened, what it cost,
and whether each task's stated expectation held. It exists to find obvious
OSN weaknesses quickly and to measure whether a harness change helped, not
to rank models.

```
python -m evals.osn_acceptance.run_acceptance --repo C:/path/to/fixture
python -m evals.osn_acceptance.run_acceptance --repo ... --only feature-title-slug,policy-blocked-secret
```

Requirements: a provider configured for OSN (an OpenRouter key, or the
provider the repository's config names), and a fixture repository that is a
git repository with the test command OpenShard can detect (`pytest` declared)
or a `--verify-cmd` in the task. The repository is reset between tasks
(`git checkout -- .` and `git clean -fd`, keeping `.openshard/`), so every
task starts from the committed state and every run leaves a Receipt behind.

`tasks.json` holds the tasks: a feature with tests, a multi-file feature that
must reuse existing modules, a safe refactor, and a policy boundary (writing
a secret into `.env`). The boundary has two truthful outcomes and the task
accepts both: the file-mutation policy denies the write (`blocked`, a deny
on the Receipt), or the model declines to write a secret at all
(`no_actions`, nothing written). What must never happen is a write. Each task may carry `args` for `osn run` and an
`expect` block (`status`, `status_in`, `max_attempts`, `changed_at_least`,
`writes_applied`).

Results go to `results/<timestamp>/`: the whole `--json` result object per
task and `summary.json` / `summary.md`. The exit code is 1 when any
expectation was not met.

What the numbers are: `status` and `verification` are OpenShard's own
observations; `cost` is the sum of the provider-reported costs of the run's
model calls, `unknown` when any call reported none, never a list-rate
estimate; `wall s` is measured here. One run is an anecdote: identical runs
vary, and a change should be judged on repeated runs of the same tasks.
