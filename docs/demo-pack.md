# OpenShard Demo Pack v0

This guide covers everything you need to record or present a 60-second or 3-minute OpenShard demo.

## What OpenShard is

Openshard is receipts for AI coding agents: what changed, whether it was independently verified, and what it cost. When Openshard runs a task itself (the native run shown in this demo), it also routes the task to a model and gates risky changes behind approval and dry-run steps.

## The demo premise

AI agents can write code and run infrastructure commands. But most tools give you the output without the audit trail — you get the diff, but not the record; the suggestion, but not the receipt. OpenShard changes that: every task goes through a controlled workflow, every action is recorded, and every proposed change can be inspected before it touches production.

## Setup

Install:

```bash
pip install openshard
```

For the flagship HarbourDocs infrastructure demo, copy the fixture outside the OpenShard
repo before recording. This prevents `openshard tui` from resolving to the OpenShard git
root instead of the HarbourDocs infrastructure repo:

```powershell
robocopy examples\production-infra-demo `
    C:\Users\Michael\HarbourDocs\harbourdocs-infra /E /NP
cd C:\Users\Michael\HarbourDocs\harbourdocs-infra
openshard tui
```

## Commands reference

| Command | Where | What it does |
|---|---|---|
| `openshard tui` | Shell | Launch the interactive TUI |
| `explain this repo` | TUI input | Ask OpenShard to summarise the repo |
| `/last` | TUI slash command | Inspect the last run from inside TUI |
| `/last more` | TUI slash command | Show full receipt from inside TUI |
| `openshard last` | Shell | Show last run receipt |
| `openshard last --more` | Shell | Full receipt with steps and tools |
| `openshard diff-last` | Shell | Unified diff of proposed changes |
| `openshard apply-last --dry-run` | Shell | Preview what would be written; no changes made |

## Layer A — Simple reproducible demo

Uses the OpenShard repo itself. Reproducible by anyone who installs OpenShard. Best for the 60-second demo.

1. Run `openshard tui`
2. Type `explain this repo` and press Enter
3. OpenShard runs this through its controlled workflow and records everything
4. In the TUI: `/last more` — or from the shell: `openshard last --more`

See: [demo-script-60s.md](demo-script-60s.md)

## Layer B — Flagship production-infra demo

Based on HarbourDocs: a fictional document-processing platform with a deliberately flawed GCP Terraform codebase. Designed to show OpenShard on serious engineering work — not a toy repo.

This demo is based on a sanitised IaC hardening scenario, not a toy repo. It covers Terraform networking, IAM, secrets, Cloud SQL, Cloud Run, storage, monitoring, state management, CI/CD readiness, and safe review before apply. The three review lenses — security for a bank-facing product, 2am operability, and developer experience for a small engineering team — make the demo relevant to real teams.

The demo repo is at [`examples/production-infra-demo/`](../examples/production-infra-demo/). The flagship task is in [`examples/production-infra-demo/demo-task.md`](../examples/production-infra-demo/demo-task.md).

To start the flagship demo, run from the HarbourDocs infrastructure repo (copy it first — see Setup above):

```powershell
cd C:\Users\Michael\HarbourDocs\harbourdocs-infra
openshard tui
```

### What this demo proves

- OpenShard can inspect production-shaped infrastructure code
- OpenShard reasons about security, operability, and developer experience simultaneously
- OpenShard produces a reviewable run record / receipt after every task
- OpenShard helps developers prioritise risks instead of blindly accepting AI output
- OpenShard supports the workflow: inspect → reason → propose → verify → record → review before apply

See: [demo-script-3min.md](demo-script-3min.md)

## Public demo note

Use fake names, fake resource IDs, fake IPs, and fake account IDs. Do not demo private customer, employer, production, or confidential infrastructure code unless it has been fully sanitised. The HarbourDocs example in `examples/production-infra-demo/` uses entirely fictional identifiers and is safe to share publicly.

## Demo task library

A structured list of demo tasks — with expected outcomes and safety notes — is in [`examples/demo-tasks.json`](../examples/demo-tasks.json).
