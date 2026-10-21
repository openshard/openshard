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
  merge_group:                    # only needed if you use a merge queue
permissions:
  contents: read
jobs:
  gate:
    name: Openshard gate          # the check name you mark as required
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0          # lets evidence for a same-content commit count (see below)
      - uses: openshard/openshard@<release tag or commit sha>
        # with:
        #   strict: true          # also fail when verification is unknown
```

Pin `@<ref>` to a release tag or a full commit sha. The action installs
Openshard from that same ref, so the gate is exactly the version you pinned.

**A pull request can change its own gate.** On `pull_request` events
GitHub runs the workflow file from the pull request's merge commit, so a
pull request that edits `.github/workflows/openshard-gate.yml` (removes the
gate, drops `strict`, swaps the action) is checked by its own edited copy.
Branch protection then only sees a green "Openshard gate" check. To enforce
the gate on everyone, require the workflow through a repository or
organisation **ruleset** ("Require workflows to pass before merging"),
which runs the workflow from the repository and ref you choose rather than from the
pull request, and review changes to `.github/` (for example with
CODEOWNERS).

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

Matching a commit with the same tree needs the head commit's objects in the
checkout. The default `actions/checkout` fetches only the temporary merge
commit (depth 1), so without `fetch-depth: 0` only evidence bound to exactly
the head commit counts.

For a `push` event, the commit that was pushed is gated.

### Merge queues

In a merge queue (`merge_group` event) GitHub tests a temporary commit that
combines the pull request with the queue ahead of it. No evidence names that
commit, so the result there is `unknown` (a warning). For that reason
`strict` is **not** applied in a merge queue: it would fail every merge. A
failed result still fails. Strict is enforced on the pull request itself, so
mark "Openshard gate" required for pull requests, and add `merge_group:` to
`on:` as above so the required check also reports in the queue (without it,
the queue waits for a check that never runs).

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
- **History committed to the repository is ignored.** It arrives with the
  change under review, so its author wrote it. When git tracks `.openshard/`
  or either file (matched case-insensitively, so `.OpenShard/` counts), when
  `.openshard/` or either file is a symlink, or when a file's real path is
  tracked, the result is `unknown` (`untrusted_history`), never `verified`.
  This applies to a committed `failed` result too: it becomes `unknown`
  (a warning, or a failure under `strict`), because evidence the author
  wrote is not used either way. A self-hosted runner that keeps its history
  behind a symlink is treated the same; use a real directory.

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
