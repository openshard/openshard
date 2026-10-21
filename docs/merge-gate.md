# Merge gate (GitHub Action)

A required check for pull requests: it fails when verification failed for
the pull request's head commit, warns when verification is unknown, and
passes when an Openshard re-run or CI observed the checks pass. It gates the
merge only. It does not change capture hooks (they stay fail-open), and it
does not stop an agent from running or committing anything.

## Add it

```yaml
# .github/workflows/openshard-gate.yml
name: Openshard gate
on:
  pull_request:
permissions:
  contents: read
jobs:
  gate:
    name: Openshard gate          # the check name you mark as required
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: openshard/openshard@<release tag or commit sha>
        # with:
        #   strict: true          # also fail when verification is unknown
```

Pin `@<ref>` to a release tag or a full commit sha. The action installs
Openshard from that same ref, so the gate is exactly the version you pinned.

| Input | Default | Meaning |
| --- | --- | --- |
| `strict` | `false` | `true` also fails on unknown |
| `commit` | the pull request head commit | the commit to gate |
| `python-version` | `3.12` | Python used to run Openshard (3.11 or later) |

Outputs: `status` (`pass`, `warn`, `fail`) and `verdict` (`verified`,
`failed`, `unknown`, `error`; see [verdict.md](verdict.md)).

## What it decides

| `openshard verdict` | Default | `strict: true` |
| --- | --- | --- |
| `verified`: an Openshard re-run or CI observed the checks pass | pass | pass |
| `failed`: the current outcome is a failure, whoever reported it | **fail** | **fail** |
| `unknown`: no evidence, partial, or only the agent says it passed | warning, check passes | **fail** |
| `error`: the question could not be answered | **fail** | **fail** |

Unknown does not block by default because a pull request with no Receipt
may simply be human-written. An error always fails with its reason: the
gate never passes because it could not run. There is no switch to make
errors pass; if you need to merge past a broken gate, a repository admin can
bypass branch protection, and that bypass is visible in GitHub.

The same decision is available locally:
`openshard ci gate --commit <sha> [--strict] [--json]`.
Exit 1 means fail; a warning exits 0.

## Which commit is gated

The pull request's **head commit** (`github.event.pull_request.head.sha`), not
the temporary merge commit GitHub checks out and not every commit in the
pull request. Verification evidence is bound to one exact commit (or to a
commit with the same tree, labelled `match: tree`; see
[verdict.md](verdict.md)). The head commit is the content being merged.
Earlier commits in the branch are work in progress: gating each of them would
block on a failure the head has already fixed. A new push changes the head,
so the gate runs again on the new commit.

For a merge queue (`merge_group`) or `push` event, the commit that triggered
the run is gated. It has no evidence of its own unless it has the same tree
as a verified commit, so expect `unknown` there.

## Where the evidence comes from, and its limits

The gate reads the same evidence `openshard verdict --commit` reads:
`.openshard/runs.jsonl` and `.openshard/verifications.jsonl` in the job's
checkout. Two consequences:

- **On a fresh GitHub-hosted runner there is usually no evidence**, because
  `openshard setup` keeps `.openshard/` out of git. The gate then reports
  `unknown` (a warning) for every pull request. It fails only when evidence
  that reached the job says verification failed. A step earlier in the same
  job that records evidence (for example on a self-hosted runner that keeps
  its history) is read.
- **History files committed to the repository are ignored.** They arrive
  with the change under review, so its author wrote them. When either file
  is tracked by git, the result is `unknown` (`untrusted_history`), never
  `verified`.

Reading the hosted Platform's Receipts from the gate is not built yet.

## The summary

The job summary (the run's Summary tab) shows the result, the agent and
model, the checks and their counts, the cost, the Receipt id and whether the
Receipt is Platform-witnessed. Anything not recorded, or not available to the
job, reads `unknown`, never zero. **Platform-witnessed** is `yes` only when
this machine holds the Platform's acknowledgement of the Receipt's capture
events. Only then does the summary say a later rewrite is detectable. On a
CI runner that acknowledgement is normally not present, so the row says
`unknown` (not `no`). Being witnessed is not being verified.

When the Receipt carries a `merge_check` block (whether its work still
merged cleanly into the default branch when it was captured; see
[per-turn-receipts.md](per-turn-receipts.md)), one line repeats it for
information. It never affects pass or fail.

## Make it required

1. Merge the workflow above into the default branch and let it run once on a
   pull request, so GitHub knows the check name.
2. **Settings, Branches** (or **Rules, Rulesets**): edit the rule for the
   default branch.
3. Turn on **Require status checks to pass before merging** and add
   **Openshard gate** (the job's `name:`).

The check name is the job name. Keep `name: Openshard gate` stable; renaming
the job renames the check, and the required check then waits forever. The
action does not post a separate check run through the Checks API: a job
already is a check run with a stable name, and `GITHUB_TOKEN` is read-only on
pull requests from forks, where a separate check run could not be created.
