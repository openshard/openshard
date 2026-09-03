# OpenShard

<p align="center">
  <strong>Receipts for AI coding agents.</strong>
</p>

<p align="center">
  AI coding agents can write code, but developers still need a clear record of what happened: what ran, what changed, what checks passed or failed, what it cost, and whether the saved record still matches its fingerprint.
</p>

<p align="center">
  OpenShard gives AI coding work a local receipt. It starts with receipts and grows into the control layer for AI coding workflows.
</p>

<p align="center">
  <strong>Agents write code. OpenShard keeps the receipt.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg?style=for-the-badge" alt="License"></a>
  <a href="#"><img src="https://img.shields.io/badge/status-alpha-orange?style=for-the-badge" alt="Status"></a>
  <a href="#"><img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python"></a>
  <a href="#"><img src="https://img.shields.io/badge/CLI-terminal-black?style=for-the-badge" alt="CLI"></a>
</p>

---

## Why OpenShard exists

AI coding agents are becoming good enough to work on real repos, infrastructure, and production-shaped systems.

That creates a new problem. Not “can the model write code?” but:

* Which model or workflow handled the task?
* What files did it inspect or touch?
* What did it change?
* Did checks pass, fail, skip, or not run?
* What did the run cost?
* Was anything risky blocked or reviewed?
* Is there a durable receipt of what happened?

OpenShard is built for the work around the agent: routing, checks, risk gates, cost awareness, feedback, local history, and Shard receipts.

The valuable unit is not a single model call. It is a completed engineering task with a record you can inspect later.

---

## What OpenShard does

OpenShard is a CLI tool for controlling and recording AI coding work.

It can:

* Run real repo tasks through a controlled execution path
* Route work across models and workflows where available
* Classify task risk
* Gate risky writes and commands
* Record model used, risk, checks, changed files, cost, and result
* Produce durable Shard receipts for runs
* Show proof, trust, and quality signals for the latest run
* Check whether a saved Shard still matches its fingerprint
* Support read-only review flows that preserve `Changed 0 files`
* Provide workflow packs for repeatable engineering reviews
* Compare models and workflows through local evals
* Track feedback and session signals around runs

OpenShard is not trying to replace Claude Code, Codex, Cursor, OpenCode, or other coding agents.

Those tools do the coding work.

OpenShard sits around them as the receipt and control layer.

---

## Current developer loop

The current local developer loop is:

```text
Ask -> Plan -> Run -> Inspect -> Feedback
```
**Ask**
Ask OpenShard product, model, command, and policy questions.

**Plan**
Generate a local execution plan. Plan Mode v1 is deterministic and conservative. It does not write files, and it does not make provider calls.

**Run**
Send a real repo task through OpenShard’s controlled execution path.

**Inspect**
Review the result, actions taken, checks, changed files, cost estimate, model choice, trust signals, and Shard receipt.

**Feedback**
Record whether the result was accepted, partial, rejected, or needs more work.

---

## Quick install

**Recommended: `pipx`**

```bash
# Install pipx first if you don't have it: brew install pipx  or  pip install pipx
pipx install openshard
openshard tui
```

**Alternative: `uv`**

```bash
uv tool install openshard
openshard tui
```

**Upgrade later:**

```bash
pipx upgrade openshard
```

See [docs/install.md](docs/install.md) for upgrade instructions and notes.

---

## Quick start with Claude Code

The fastest way to get value from OpenShard is to let it quietly record the Claude Code work you already do. No API key, account, or cloud service is needed — everything stays in the repository.

```bash
pipx install openshard   # 1. Install (once per machine)
cd my-project            # 2. Go to a git repository
openshard setup          # 3. Set up (once per repository)
```

`openshard setup` detects your environment, configures Claude Code for this repository, and ends with:

```text
OpenShard is ready. Use Claude Code normally.

Next steps:
  1. Open Claude Code in this repository.
  2. Complete a normal coding task.
  3. Run `openshard last` to see the captured Shard receipt.
```

That is the whole loop: use Claude Code as you normally would, then run `openshard last` to see the receipt of what happened.

Useful follow-ups:

```bash
openshard doctor                 # Is OpenShard actually working here? (✓/✗ checklist)
openshard setup                  # Safe to re-run; already-configured parts are left alone
openshard mcp uninstall claude   # Remove OpenShard's Claude Code configuration (history is kept)
```

If `setup` reports a limitation — most commonly a custom Claude Code status line already in place — it tells you exactly what stays unavailable (model/cost/token data on receipts) and the one step to enable it. It never replaces your existing settings.

Under the hood, `setup` registers a local, read-only MCP server so Claude can look up your history, installs Claude Code hooks that record sessions as Shards, and configures the status line for receipt enrichment. You do not need to understand any of that to use it; the lower-level `openshard mcp install claude` command remains available if you want to.

---
Launch the TUI:

```bash
openshard tui
```

Inside the TUI:

```text
/ask what models do you support?
/plan review this repo for production readiness
/packs
/pack production-iac-hardening
```

Run a real repo task:

```text
Review and harden this deliberately flawed Terraform codebase. Assess it through security/compliance posture, 2am operability, and developer experience for a 5-10 person engineering team. Identify critical, high, and medium risks. Explain trade-offs. Do not apply changes directly without review.
```

Inspect the latest run:

```text
/last more
```

Or from the shell:

```bash
openshard last --more
```

The `--more` view includes a PROOF SUMMARY block when OSN proof metadata is present, showing observation, progress, verification, loop, retry, and PR comment status.

Optional local follow-up commands after a run:

```bash
openshard reflect last                        # advisory reflection on the run (local, no model calls)
openshard pr comment                          # generate a GitHub-ready PR comment from the run
openshard pr comment --output pr-comment.md  # write the PR comment to a file
```

Leave feedback:

```bash
openshard feedback --outcome accepted --reason "Useful review"
```

See the demo scripts for a recorded walkthrough:
- [docs/demo-script-60s.md](docs/demo-script-60s.md)
- [docs/demo-script-3min.md](docs/demo-script-3min.md)

---

## Production IaC demo

The `examples/production-infra-demo/` directory contains a fictional GCP workload called **DocuVault** — a sanitised demo scenario for OpenShard.

The infrastructure is intentionally production-shaped: networking, IAM, Cloud SQL, Cloud Run, storage, secrets, monitoring, and logging.

It is deliberately flawed to serve as the input for an infrastructure-as-code hardening review.

All names, project IDs, resource IDs, CIDRs, and accounts are fake and public-safe. No employer or customer details. Designed to show a serious IaC review, not a toy example.

See:
- [`examples/production-infra-demo/README.md`](examples/production-infra-demo/README.md)
- [`examples/production-infra-demo/demo-task.md`](examples/production-infra-demo/demo-task.md)

A typical production IaC review can show:
- Critical, high, and medium findings
- File-level evidence such as `iam.tf`, `secrets.tf`, `database.tf`, `network.tf`, and `storage.tf`
- Verification output from tools like `terraform fmt`, `terraform validate`, and `tflint` when available
- A clear `Changed 0 files` receipt for read-only reviews
- Model selection and cost tracking
- A `/last more` view with the full Shard, findings, checks, evidence, and cost comparison

This is the core OpenShard use case: let AI help with serious engineering work, but keep the control, evidence, and receipt layer visible.

---

## Shard receipts

A Shard is the saved record of an AI coding run.

New here? Read [What is a Shard?](docs/what-is-a-shard.md).

Think of it like a receipt for AI coding work.

It can show:

* Task and agent
* Model used
* Strategy
* Risk level
* Context used, when recorded
* Inspected files
* Changed and touched files
* Checks and their outcomes
* Findings, when structured findings exist
* Cost
* Actions timeline
* Result
* Trust score
* Content hash status

A Shard does not prove the code is perfect. Nothing can.

What it proves is more practical: what OpenShard recorded during the run, what changed, what checks passed or failed, whether anything risky was blocked, and whether the saved record changed later.

OpenShard can also record feedback and infer session signals around a run.

```bash
openshard last --more    # expanded receipt for the latest run
openshard last --full    # full stored details
openshard proof last     # inspect the latest run's proof
openshard trust last     # inspect the latest run's trust score
```

Every Shard receipt can power two local follow-up commands:

```bash
openshard reflect last                        # local advisory reflection on the run
openshard pr comment                          # generate a GitHub-ready PR comment
openshard pr comment --output pr-comment.md  # write the PR comment to a file instead
```

Both commands are local and deterministic. They do not make additional model calls.

Raw developer content is not stored by default.

---

## One run, end to end

A normal OpenShard run can capture:

1. **Task** - the user request or workflow pack prompt.
2. **Routing** - which model or workflow was selected.
3. **Risk** - whether the task is low, medium, high, or requires stronger review.
4. **Execution** - what happened during the run.
5. **Checks** - verification results, including passed, failed, skipped, or not run.
6. **Recorded context** - files inspected, findings, and relevant source references when available.
7. **Changes** - files changed, touched, or left untouched.
8. **Cost** - estimated spend for the run.
9. **Receipt** - a durable Shard record that can be inspected later.
10. **Fingerprint** - a content hash that helps detect whether the saved record changed later.

The point is simple: every AI coding run should leave behind a receipt that a developer or team can inspect.

---

## How OpenShard is different

OpenShard is not a chatbot, IDE, or even a generic agent framework.
It's the layer around agentic coding work.

| Layer | What it does |
|---|---|
| Coding agent | Generates code, edits files, answers task prompts |
| Model router | Chooses which model or workflow should handle the job |
| Verification layer | Runs checks and records whether they passed, failed, skipped, or were not run |
| Policy layer | Gates risky writes, commands, and high-risk work |
| Receipt layer | Records model, cost, evidence, checks, changed files, and result |
| Eval layer | Compares models and workflows by outcome, cost, speed, and safety |

OpenShard can work alongside tools like Claude Code, Codex, Cursor, OpenCode, LangChain, LangGraph, OpenRouter, and provider APIs.

The goal is not to replace every coding agent.
The goal is to make AI coding work controllable, inspectable, and measurable.

---

## Workflow packs

Workflow packs are pre-built prompts for repeatable engineering reviews.
```bash
openshard packs list
openshard packs show production-iac-hardening
openshard packs prompt production-iac-hardening
```

Built-in packs include:
- `repo-explanation`
- `production-iac-hardening`
- `terraform-networking-review`
- `iam-security-review`
- `cicd-safety-review`
- `powershell-automation-review`

Workflow packs make common review patterns repeatable without forcing users to rewrite long prompts every time.

---

## Command reference
Set up and check Claude Code capture:

```bash
openshard setup                                    # Configure Claude Code capture for this repo (safe to re-run)
openshard setup --json                             # Same, with a machine-readable result
openshard setup --agent --json                     # Read-only status snapshot; never writes (CI/agents)
openshard doctor                                   # Health check: repo, history, Claude Code, MCP, hooks, enrichment
openshard mcp install claude                       # Lower-level: MCP server + hooks + status line only
openshard mcp uninstall claude                     # Remove OpenShard's Claude Code config; history is never deleted
```
Most developers who want the interactive experience should start with the TUI:

```bash
openshard tui                                      # Launch the OpenShard terminal UI
```
Run tasks:

```bash
openshard run "Review this repo for risks"         # Run a task through OpenShard from the shell
openshard run --workflow native "Fix this bug"     # Run using the native workflow path
```
Inspect the latest run:

```bash
openshard last                                     # Show the latest run summary
openshard last --more                              # Show the expanded Shard receipt
openshard last --full                              # Show full stored/debug details
```
Reflect and export:

```bash
openshard reflect last                             # Advisory reflection on the last run (local, no model calls)
openshard pr comment                               # Generate a GitHub-ready PR comment from the last run
openshard pr comment --output pr-comment.md        # Write the PR comment to a file
```
Record feedback:

```bash
openshard feedback --outcome accepted              # Mark the latest run as accepted
openshard feedback --outcome partial               # Mark the latest run as partly useful
openshard feedback --outcome rejected              # Mark the latest run as not useful
openshard feedback --outcome abandoned             # Mark the latest run as abandoned
openshard feedback --outcome accepted --reason "kept as-is"  # Optionally include a free-text reason
```
Infer local session signals:

```bash
openshard session infer                            # Infer local behavioural/session signals from run history
```
Workflow packs:

```bash
openshard packs list                               # List available workflow packs
openshard packs show production-iac-hardening      # Show details for a workflow pack
openshard packs prompt production-iac-hardening    # Print the pack prompt
```
Model registry and policy:

```bash
openshard models list                              # List registered models
openshard models role reasoning                    # Show reasoning-capable models
openshard models role cheap_control                # Show low-cost/control models
openshard models mode ask                          # Show Ask Mode model policy
openshard models mode plan                         # Show Plan Mode model policy
```
Local evals:

```bash
openshard eval list                                # List eval suites
openshard eval validate --suite basic              # Validate an eval suite
openshard eval run --suite basic                   # Run an eval suite
openshard eval report                              # Show latest eval report
openshard eval compare                             # Compare models by eval results
openshard eval stats                               # Show eval stats
```
Useful TUI commands:

```text
/ask what models do you support?                   # Ask OpenShard product/model questions
/plan review this repo for production readiness    # Generate a local plan without writing files
/packs                                             # List workflow packs inside the TUI
/pack production-iac-hardening                     # Load a workflow pack inside the TUI
/last                                              # Show the latest run
/last more                                         # Show expanded run details
/last full                                         # Show full debug/audit details
/feedback accepted                                 # Record feedback for the latest run
/clear                                             # Clear the output panel
/quit                                              # Exit the TUI
```
After a run completes, the TUI shows command hints for `openshard reflect last` and `openshard pr comment`.

---

## What works today

OpenShard is still alpha, but the core local loop is working.

Current features include:

* Local CLI and TUI (`openshard tui`)
* Ask Mode for local product/model/command Q&A
* Plan Mode v1 for deterministic local plans
* Controlled run path for real repo tasks
* OpenShard Native execution harness
* Task classification and risk handling
* Model registry and model policy inspection
* Routing across models/workflows where available
* Shard receipts with model, risk, files, checks, cost, result, and trust signals
* `/last`, `/last more`, and `/last --full`
* `openshard proof last` for latest-run proof inspection
* `openshard trust last` for latest-run trust scoring
* Shard quality summary in `last --json`
* Compact `Proof: <status>` line in `openshard last`
* Content hash verification for Shards
* Best-effort pre-send secret scanning before provider calls
* Safer JSONL history writes with write locking
* CI check mode for pass / warn / fail / skip decisions
* GitHub Actions PR receipt output surfaces
* Read-only review handling that preserves `Changed 0 files`
* Intent-specific review handling for Terraform/IaC, CI/CD, auth/security, tests, and docs/onboarding
* Workflow packs for repeatable engineering reviews
* Feedback signals
* Session signal inference
* Local run history
* Local eval harness
* Eval comparison by pass rate and cost-per-pass
* Cost comparison in `/last more`
* OSN proof pipeline with PROOF SUMMARY in `openshard last --more` when metadata is present
* `openshard reflect last` for local advisory run reflection
* `openshard pr comment` for local GitHub PR comment generation
* TUI post-run command hints for reflect and pr comment
* Production-shaped Terraform demo
* 6,800+ passing tests and green CI

---

## What is not built yet

OpenShard is early and intentionally local-first.

Not built yet:

* No hosted team platform yet
* No cloud sync yet
* No hosted dashboard for teams yet
* No IDE integration yet
* No Homebrew, winget, or one-line shell installer yet
* Ask Mode and Plan Mode are local deterministic v1 flows
* Feedback advisory does not automatically change routing yet
* Model lifecycle tags do not yet drive default routing behavior
* External Claude Code, Codex, Cursor, and OpenCode receipt capture is not fully implemented yet
* External harness adapters are experimental and not guaranteed
* Not a full Claude Code, Codex, Cursor, or OpenCode replacement

---

## Current validation state

OpenShard is still early, but it is not just a prototype.

Current validation includes:

* 6,800+ passing tests
* Green CI
* Ruff-clean Python codebase
* Clean `pipx install openshard` path from PyPI
* Local CLI/TUI workflow
* Production-shaped Terraform demo
* Workflow packs for repeatable reviews
* Shard receipts for run history
* Proof, trust, quality, and hash checks for run records
* CI check surfaces for automation
* Eval tooling for model and workflow comparison
* Pre-launch usage from developers testing it on real work

The project is alpha, but the core loop is working:

```text
Run the task -> inspect what happened -> verify the output -> keep the receipt
```

---

## Roadmap

Near-term roadmap:

* More real-world developer testing
* Better external-agent receipt capture, starting with Claude Code
* Better repo-aware planning
* Stronger model/workflow ranking from real outcomes
* More workflow packs
* More repo analyzers for common stacks
* Cleaner setup and release packaging
* Hosted/team run history
* Team policies and shared approval gates
* Dashboards for cost, model usage, and verification outcomes

Longer-term, OpenShard should become the control layer teams use to manage AI engineering work.

---

## Why open source?
Routing decisions should be inspectable.

If a tool decides which model touches security-sensitive code, developers should be able to see why.

OpenShard is open because trust, integrations, and routing policies improve when real users can inspect and extend the system.

Open source also keeps the local-first layer useful on its own. Hosted and team features can come later, but the core control layer should be understandable and inspectable.

---

## Contributing

Contributions are welcome around:
- Routing policies and scoring logic
- Repo analyzers for new stacks
- Model profiles and capability data
- Evaluation datasets
- Provider integrations
- Workflow packs
- CLI/TUI UX improvements
- Documentation and examples

See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

---

## Security
If you find a security issue, please report it privately before opening a public issue.

See [SECURITY.md](SECURITY.md).

---

## License
Apache-2.0
