# Historical Receipt recovery: read-only inventory and dry run (10 October 2026)

No production Receipt was changed. This records what the available evidence
proves, the tools that reproduce it, and what cannot be recovered.

## Tools

- `scripts/receipt_recovery_github_evidence.py` reconstructs, for every
  Openshard Cloud Receipts workflow run, which commits were inspected. For each
  commit it records the explicit metadata the run would read (commit trailers,
  then the PR body), the deterministic hosted Receipt id, and linked Claude Code
  session records.
- `scripts/receipt_recovery_inventory.py` measures hosted Receipt completeness
  with GET requests only and writes a dry-run plan. Each proposed action names
  its evidence and why it is trustworthy. It never proposes changing a sealed
  Receipt; recovered values would be added to the append-only usage-evidence
  ledger.
- Tests: `tests/test_receipt_recovery_scripts.py`.

Inputs for the evidence script are gathered read-only (workflow runs and PRs
from the GitHub REST API, Claude Code session records from the session API).
They are not committed: session records contain private account usage.

## Production inventory: not yet run

The only production credential available on 10 October was a connected-capture
key, which gets HTTP 403 on every Receipt read route. Exact production
completeness figures need a read-only organisation API key
(`OPENSHARD_API_KEY`, `OPENSHARD_ORG_ID`); none are estimated here.

## What GitHub proves

The figures cover every workflow run since the workflow began on 1 October,
the full history of both repositories and every PR body.

| | Core | Platform | Total |
|---|---|---|---|
| Workflow runs | 423 | 372 | 795 |
| Commits inspected | 292 | 231 | 523 |
| With agent metadata (Receipt or CI attachment expected) | 108 | 199 | 307 |
| Skipped: no agent metadata | 184 | 32 | 216 |

The 307 expected GitHub cloud Receipts declare:

- agent: 307;
- model: 253 (54 missing: Codex 40, Claude Code 13, chatgpt-cloud 1);
- tokens: 30, all `claude-fable-5-1`;
- cost: 0.

Early workflow versions defaulted the agent only on manual dispatch, and every
dispatch ran a later version, so no Receipt was given a default agent.

## Evidence that can recover fields

Each Claude Code session record carries the session's configured model; 13
cloud sessions also carry runtime token and estimated-cost totals. Those totals
are session-wide runtime estimates, not billed charges.

| Proposed action | Count | Why |
|---|---|---|
| Attach model `claude-fable-5-1` (model only) | 8 | The commit links exactly one session. The commit time is inside that session, the repository is in its scope, and no model fallback is recorded. |
| Model: needs review | 5 | The commit (a later squash merge) falls about 40 minutes after the session ended. |
| Model: not recoverable | 41 | No Codex or ChatGPT run record is available. |
| Tokens or cost | 0 | The linked sessions produced 4 to 30 commits each; attaching a session total to one Receipt would double-count. |
| Agent for the four unattributed backfills | 0 | No session or metadata evidence exists. |

## Cause found for future capture

Claude Code cloud sessions with several repositories start in the parent
directory, so the per-checkout hooks the setup script installed never loaded.
Their commits carry a `Claude-Session` trailer but no `Openshard-Agent`
metadata. Workspace capture (`openshard capture install claude --workspace`)
and attach-only CI delivery address this; see `docs/claude-cloud-prelaunch.md`.

## Limits

- PR bodies are read as they are now, not as of each run.
- Multi-commit push commit sets are reconstructed from git ancestry.
- Per-run delivery outcomes cannot be read in bulk without production access.
