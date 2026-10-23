# Merge gate (GitHub Action)

A required check for pull requests that reads the head commit's Receipt from
the Openshard Platform and turns it into a plain pass or fail. There is no
pass-with-warning. It gates the merge only: capture hooks stay fail-open and
nothing stops an agent from running or committing.

## Add it

```yaml
# .github/workflows/openshard-gate.yml
name: Openshard gate
on:
  pull_request:
  merge_group:                    # only if you use a merge queue (see below)
permissions:
  contents: read
  id-token: write                 # a read-only OIDC token for the Platform
jobs:
  gate:
    name: Openshard gate          # the check name you mark as required
    runs-on: ubuntu-latest
    timeout-minutes: 20           # at least wait-minutes + 5
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0          # the PR's commits, to look for agent marks
      - uses: openshard/openshard@<release tag or commit sha>
        # with:
        #   wait-minutes: 15
```

The repository must be connected to an Openshard organisation through the
Openshard GitHub App, the same link cloud capture uses. Pin `@<ref>` to a
release tag or a full commit sha; the action installs Openshard from that
ref.

| Input | Default | Meaning |
| --- | --- | --- |
| `wait-minutes` | `15` | How long to wait while verification is pending, or while an agent change has no Receipt yet, then fail |
| `api` | `https://api.openshard.dev` | Platform API base |
| `api-key`, `org` | none | Organisation API key (from a secret) and organisation id, for CI without GitHub OIDC |
| `commit` | the pull request head commit | The commit to gate |
| `strict` | none | Deprecated, no effect: the gate only passes or fails |
| `python-version` | `3.12` | Python used to run Openshard (3.11 or later) |

Outputs: `status` (`pass` or `fail`) and `verdict` (the Platform's answer:
`verified`, `failed`, `pending`, `none`; or why there was none:
`unreachable`, `auth`, `fork`, `no_credentials`, `timeout`, `bad_response`,
`rejected`, `error`).

The same decision runs locally or in other CI:
`openshard ci gate --commit <sha> [--base <sha>] [--wait-minutes N]`, with a
GitHub OIDC environment or `OPENSHARD_GATE_API_KEY` and `OPENSHARD_GATE_ORG`
set. Exit 0 pass, 1 fail.

## What it decides

The gate asks the Platform
`GET /v1/cloud/github/gate-verdict?sha=<head sha>` (OIDC audience
`openshard-gate`; the API-key variant is
`GET /v1/orgs/<org>/commits/<sha>/gate-verdict`). The Platform finds the
Receipts whose verification is bound to that commit.

| Platform answer | Gate |
| --- | --- |
| `verified` | **pass** |
| `failed` | **fail** |
| `pending` (a Receipt exists, verification not final) | waits up to `wait-minutes`, asking again every 15 s; then pass or fail. Still pending: **fail**. With a Receipt the reason is "Verification incomplete (partial) after N min" and names its verification state (a partial or agent-reported pass is never `verified`); without one, "Verification did not finish in N min" |
| `none`, and an agent mark on the pull request's commits | waits up to `wait-minutes`, asking again every 15 s, because the Receipt may still be on its way; a Receipt that appears is decided as above. Still none: **fail**: "an agent changed code but there is no Receipt", with how long the gate waited |
| `none`, no agent mark, team policy `agent_prs` | **pass** at once: "Human change — no agent activity recorded" |
| `none`, no agent mark, team policy `all_prs` | **fail** at once |
| `none`, and the commits could not be read | **fail** (check out with `fetch-depth: 0`) |
| Platform unreachable, credentials refused, no credentials, malformed answer | **fail**, with the reason |
| Pull request from a fork | **fail**: "Fork PRs can't be verified: GitHub gives them no credentials" |

Transient failures (network, HTTP 429 or 5xx) are retried within
`wait-minutes`; a refused credential fails at once. A fresh OIDC token is
minted for every request.

**Why an agent change waits.** On a push, the gate and the workflow that
creates the Receipt (for example Openshard Cloud Receipts, which runs the
checks first) start together, so the gate's first answer is often `none`.
The gate keeps asking within `wait-minutes`. A human change has nothing to
wait for and is decided at once. The default of 15 minutes is set from
Openshard's own Cloud Receipts runs (push to Receipt delivered: median 4.7,
p90 8.5, longest 9.8 minutes over 50 runs; a `Claude-Session` commit can
add up to 3 more). Set `wait-minutes` above the time your Receipt workflow
takes, and keep the gate job's `timeout-minutes` at least `wait-minutes`
+ 5, or the job is cancelled before it can report.

While it waits the gate prints one aligned, timestamped line a minute to
the log (stderr), folded in a group, then a closing line with the decision:

```
::group::Openshard gate  ·  waiting up to 15m for the Platform  ·  Codex
10:00:20Z  Openshard gate  ·  waiting for receipt       ·  Codex             ·  none         ·   0m / 15m
10:01:20Z  Openshard gate  ·  waiting for receipt       ·  Codex             ·  none         ·   1m / 15m
10:04:20Z  Openshard gate  ·  waiting for verification  ·  Codex             ·  pending      ·   4m / 15m
::endgroup::
10:04:35Z  Openshard gate  ·  passed                    ·  Codex             ·  verified     ·   4m / 15m
```

In an interactive terminal it shows the same fields as a spinner instead,
unless `NO_COLOR` is set or `TERM` is `dumb`. The gate's stdout (the
`Openshard gate: pass` line and the headline) is unchanged.

**Agent marks** are the trailers Openshard already defines, in any commit
from the pull request's base to its head, or in the pull request body:
`Openshard-Agent` and the other `Openshard-*` metadata, the usage trailers
`openshard usage trailers` writes (`Openshard-Tokens-*`,
`Openshard-Usage-Scope`), and `Claude-Session: https://claude.ai/code/session_...`.
A mark only makes the gate stricter, so a claimed session counts without the
Platform corroborating it.

**Team policy** `require_receipt` (`agent_prs` by default, or `all_prs`) is a
Platform organisation setting returned with every answer. It is not an input
here, so a pull request cannot change it.

**This runner's own evidence.** When the Platform has no Receipt and this
job's checkout holds trusted `.openshard/` evidence for the commit (a
self-hosted runner that keeps its history), a conclusive local verdict
(`verified` or `failed`) decides. Platform evidence always wins when there
is any. History committed to the repository is never used: if git tracks
`.openshard/` or either evidence file (case-insensitively), if any of them
is a symlink, or if a file's real path is tracked, the local evidence is
ignored. That holds for a committed `failed` result too.

## Change checks (information only)

The summary also lists fixed, model-free findings about the pull request
itself, read from the checkout between the base's merge-base and the head
(`openshard/verification/pr_findings.py`). They are shown for the reviewer
and do not change pass or fail.

| Finding | What it means |
| --- | --- |
| `large_change` | over 400 lines added plus removed, not counting lockfiles; consider a split |
| `junk_files` | added backups, merge leftovers, logs, bytecode or OS metadata files |
| `grader_touched` | existing tests edited, deleted or renamed, or CI, lint, type-check, test or coverage settings changed |
| `fix_by_deletion`, `removed_public_code` | public functions or classes, endpoints or tests removed; `fix_by_deletion` when a commit subject or the title reads as a bug fix |
| `new_dependencies` | packages added to `pyproject.toml`, `requirements*.txt` or `package.json`, and the lockfiles changed. The gate does not look up publish dates; `openshard ci findings --registry-lookup` does, sending the package names to PyPI or npm |

Without a base commit (and `fetch-depth: 0`) they read "not checked". The
same findings are in `--json` as `change_findings`.

## Getting Receipts to the Platform in time

Receipts reach the Platform through the capture service's sync (on every
Receipt seal, and every 5 minutes). The automatic per-commit verification
(`openshard verify --commit --auto`, started when a session's commit is
observed) now sends that Receipt and its new commit evidence immediately,
when the machine is linked (`openshard login`) and sync is not turned off.
A failure there never affects the developer; the gate's `pending` wait and
timeout cover the gap.

Known gap: a Receipt is sealed when the agent's turn ends. If an agent
commits and pushes in the same turn and the gate runs before the turn ends,
the Platform can have no Receipt yet. A commit with agent marks then waits
up to `wait-minutes` for it and fails if it has not arrived (re-run the check
once the Receipt has synced); a commit with no marks under `agent_prs`
passes as a human change.

## Which commit is gated

The pull request's **head commit**, not GitHub's temporary merge commit and
not each commit in the branch. Verification evidence is bound to one exact
commit; the head is the content being merged. A new push changes the head
and the gate runs again. For a `push` event, the pushed commit is gated.

### Merge queues

In a merge queue (`merge_group`) GitHub tests a temporary commit that
combines the pull request with the queue ahead of it. The Platform reads the
pull request head from the queue ref in the signed OIDC token and answers
for that head; the answer names both (`merge_group`: the requested queue
commit, the pull request number and its head), and the summary says
"Merge queue: PR #N head <sha>". The gate accepts that answer only when it
names exactly the queue commit it asked about. When the queue ref names no
pull request head, the answer is `none` with a `reason`, shown in the
summary. Add `merge_group:` to `on:` so the required check reports in the
queue at all (without it the queue waits for a check that never runs).
Agent marks are read from the queue's base to the queue commit, which
includes the pull request's commits.

## The summary

The job summary (the run's Summary tab) reads like the receipt card in
Openshard: a one-line verdict (`Openshard gate · Passed` or `· Failed`), the
reason, the commit and the Platform's answer. Then, for every Receipt the
Platform returned, a two-column table in the card's order: Result (status,
the verification result when it differs, the commit it is bound to and
when), Agent, Model, Authorship (when the Platform sends it), Task, Changes
(files, lines added and removed), Checks (`2/2 passed`, or each outcome
named), Cost (`~` marks an estimate, with its source), Witnessed (and
whether the signature verified) and a link to the Receipt. Each check with
its outcome and evidence source, the approvals, and the Receipt's
`merge_check` (information only) are folded under "Checks and approvals".
Each field appears once. Anything the Platform does not record reads
`unknown`, never zero. Only a witnessed Receipt is described as having a
detectable rewrite, and being witnessed is not being verified. All text is
escaped before it is written.

## Make it required, and keep it required

1. Merge the workflow into the default branch and let it run once on a pull
   request, so GitHub knows the check name.
2. **Settings, Branches** (or **Rules, Rulesets**): edit the rule for the
   default branch, turn on **Require status checks to pass before merging**
   and add **Openshard gate** (the job's `name:`). Keep that name stable.

**A pull request can change its own gate.** On `pull_request` events GitHub
runs the workflow file and `uses:` reference from the pull request's merge
commit, so a pull request that edits `.github/workflows/openshard-gate.yml`
is checked by its own edited copy. To enforce the gate on everyone, require
it through a repository or organisation **ruleset** ("Require workflows to
pass before merging"), which runs the workflow from the repository and ref
you choose, and review changes to `.github/` (for example with CODEOWNERS).

The action does not post a separate check run through the Checks API: the
job already is a check run with a stable name, and `GITHUB_TOKEN` is
read-only on fork pull requests.
