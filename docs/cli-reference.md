# CLI reference

This is a task-oriented tour of the Openshard CLI, moved out of the README to
keep that focused on the beginner flow (`openshard setup` -> use your coding
agent -> `openshard last`).

For the full, always-current list of commands, run `openshard --help` (root
commands, grouped) or `openshard <group> --help` for any group. This document
is a curated subset with examples; it does not enumerate every command.

Set up and check Claude Code capture:

```bash
openshard setup                                    # Configure Claude Code capture for this repo (safe to re-run)
openshard setup --json                             # Same, with a machine-readable result
openshard setup --agent --json                     # Read-only status snapshot; never writes (CI/agents)
openshard doctor                                   # Health check: repo, history, Claude Code, MCP, hooks, enrichment
openshard mcp install claude                       # Lower-level: MCP server + hooks + status line only
openshard capture rotate-token                     # Replace the local capture token (then re-run setup per repo)
openshard mcp uninstall claude                     # Remove Openshard's Claude Code config; history is never deleted
```

Grok Bot (Cursor's cloud teammate; see [grok-bot.md](grok-bot.md)):

```bash
openshard grok-bot ingest export.pb --repo PATH    # Enterprise: ingest an OTLP logs export of Action Recording (protobuf or OTLP/JSON)
openshard grok-bot serve --repo PATH               # Enterprise: OTLP/HTTP receiver; needs OPENSHARD_GROK_BOT_OTLP_TOKEN
openshard grok-bot skill                           # Print the self-report skill to give to the Bot
openshard grok-bot report -                        # Record a Bot self-report (JSON on stdin) as an agent_reported Shard
```

Most developers who want the interactive experience should start with the TUI:

```bash
openshard tui                                      # Launch the Openshard terminal UI
```

Run tasks:

```bash
openshard run "Review this repo for risks"         # Run a task through Openshard from the shell
openshard run --workflow native "Fix this bug"     # Run using the native workflow path
```

Inspect what Openshard captured (local, offline, works from any subdirectory of the repo):

```bash
openshard last                                     # Show the latest run summary
openshard last --more                              # Show the expanded receipt
openshard last --full                              # Show full stored/debug details
openshard verify                                   # Re-run approved checks for the latest receipt (OpenShard-observed evidence)
openshard verify --dry-run                         # Show which checks would run, and their safety class
openshard verify --from-observed --approve         # Also re-run the agent's observed checks; allow needs-approval commands
openshard history                                  # Recent receipts for this repo, newest first
openshard history --limit 20 --json                # Same, more rows, machine-readable
openshard context "fix the flaky auth test"        # What relevant_context would give an agent, and why
openshard context --text "fix the flaky auth test" # Just the block an agent would receive
openshard stats                                    # Counts over recorded receipts (agents, models, checks, est. cost)
openshard stats completeness                       # Receipt completeness heuristic
openshard stats failures                           # Failure categories over recent runs
```

Reflect and export:

```bash
openshard reflect last                             # Advisory reflection on the last run (local, no model calls)
openshard pr comment                               # Generate a GitHub-ready PR comment from the last run
openshard pr comment --output pr-comment.md        # Write the PR comment to a file
```

Record feedback:

```bash
openshard feedback accept                          # Mark the latest run as accepted
openshard feedback reject --reason "..."           # Mark the latest run as rejected
openshard feedback retry --reason "..."            # Mark the latest run as needing a retry
openshard feedback note "kept as-is"                # Add a free-text note
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
/ask what models do you support?                   # Ask Openshard product/model questions
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
