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
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0          # the PR's commits, to look for agent marks
      - uses: openshard/openshard@<release tag or commit sha>
        # with:
        #   wait-minutes: 10
```

The repository must be connected to an Openshard organisation through the
Openshard GitHub App, the same link cloud capture uses. Pin `@<ref>` to a
release tag or a full commit sha; the action installs Openshard from that
ref.

| Input | Default | Meaning |
| --- | --- | --- |
| `wait-minutes` | `10` | How long to wait while verification is pending, then fail |
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
| `none`, and an agent mark on the pull request's commits | **fail**: an agent changed code but there is no Receipt |
| `none`, no agent mark, team policy `agent_prs` | **pass**: "Human change — no agent activity recorded" |
| `none`, team policy `all_prs` | **fail** |
| `none`, and the commits could not be read | **fail** (check out with `fetch-depth: 0`) |
| Platform unreachable, credentials refused, no credentials, malformed answer | **fail**, with the reason |
| Pull request from a fork | **fail**: "Fork PRs can't be verified: GitHub gives them no credentials" |

Transient failures (network, HTTP 429 or 5xx) are retried within
`wait-minutes`; a refused credential fails at once. A fresh OIDC token is
minted for every request.

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
the Platform can have no Receipt yet. A commit with agent marks then fails
(re-run the check once the Receipt has synced); a commit with no marks under
`agent_prs` passes as a human change.

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

The job summary (the run's Summary tab) shows the decision and, for every
Receipt the Platform returned: a link to it in Openshard, status, agent,
model, task, files changed, lines added and removed, each check with its
outcome and evidence source, the verification result with when and which
commit it is bound to, cost with its source, approvals, whether the Platform
witnessed the capture, whether its signature verified, and the Receipt's
`merge_check` (whether its work merged cleanly into the default branch at
capture) as information only. Anything the Platform does not record reads
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
