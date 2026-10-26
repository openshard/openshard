<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/openshard/openshard/main/docs/assets/openshard-wordmark-white.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/openshard/openshard/main/docs/assets/openshard-wordmark-black.png">
    <img src="https://raw.githubusercontent.com/openshard/openshard/main/docs/assets/openshard-wordmark-black.png" alt="Openshard" width="560">
  </picture>
</p>

<h3 align="center">Receipts for AI coding agents.</h3>

<p align="center">
  <strong>Openshard is an open-source tool that keeps a record of everything your AI coding agents do.</strong>
</p>

<p align="center">
  Each time Claude Code, Codex, Cursor or another agent works in your repo, Openshard writes a receipt:<br>
  which files it changed, whether the tests really passed, and what it cost.<br>
  Teams can also block agent work from merging until its receipt proves it.
</p>

<p align="center">
  <a href="https://pypi.org/project/openshard/"><img src="https://img.shields.io/pypi/v/openshard?label=PyPI" alt="PyPI"></a>
  <a href="https://github.com/openshard/openshard/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache 2.0"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/macOS%20%C2%B7%20Linux%20%C2%B7%20Windows-tested-success" alt="macOS, Linux, Windows">
</p>

<p align="center">
  <a href="https://openshard.dev">Website</a> ·
  <a href="https://github.com/openshard/openshard/tree/main/docs">Docs</a> ·
  <a href="https://openshard.dev/resources/changelog">Changelog</a> ·
  <a href="https://openshard.dev/pricing">Pricing</a>
</p>

---

## Start in one minute

```bash
pipx install openshard     # or: uv tool install openshard
cd your-project
openshard setup            # finds your agents and sets them up
```

Now use Claude Code, Codex, Cursor or any [supported agent](#works-with) exactly as you do today. When it finishes:

```bash
openshard last             # the receipt for that run
openshard verify           # re-run your checks yourself, so it's proven, not just claimed
```

No account, no API key, nothing to configure. Receipts stay in your repo.
Stuck? `openshard doctor` shows what's working, agent by agent.

## What you get

Here is a real receipt for a Claude Code change, before and after `openshard verify` (trimmed to the key rows):

```text
  Task        Round prorated seat charges down so nobody is overcharged
  Executor    Claude Code
  Model       Claude Opus 5.5
  Changed     2 files (billing.py, test_billing.py)
  Checks      1/1 passed (agent-reported)
  Verified    Not verified by OpenShard (agent reported 1/1 passed)
```

```text
  Verified    Passed (OpenShard re-ran the check(s): 1/1 passed @ a6f1dfbb1950)
  Signature   Signed · key 8257ca3e
```

Every receipt answers the questions you'd ask before merging:

| Question | What the receipt shows |
| --- | --- |
| **What changed?** | Every file the agent touched, checked against git. |
| **Did it really work?** | The agent's own claim is marked as a claim. It only becomes **verified** when Openshard re-runs your checks or CI passes on that exact commit. |
| **What did it cost?** | The agent, model, tokens and cost, marked as an estimate when it is one. |
| **What's missing?** | If something couldn't be recorded, it says so. It never guesses or fills a gap with zero. |
| **Was it edited later?** | Every receipt is signed. Any later change shows up. |

## Block unproven agent work from merging

The **merge gate** is a GitHub check that passes or fails each pull request using its receipt. Verified work passes. Failed checks, unfinished verification, or agent changes with no receipt fail. There is no "pass with warning".

Agent commits are marked in git (an `Openshard-Agent` trailer), so the gate can tell agent work from human work. Today that mark is added automatically for Claude Code; you can turn it off with `openshard setup --no-commit-trailers`.

The gate uses the hosted dashboard, so it needs a Pro or Team plan. Setup takes one workflow file: [docs/merge-gate.md](docs/merge-gate.md).

## Works with

| Agent | How it connects |
| --- | --- |
| Claude Code, Codex, Cursor, OpenCode | Set up automatically by `openshard setup` |
| Google Antigravity, Grok Build | Their own hooks, set up by `openshard setup` (Grok Build asks you to trust the folder once) |
| Hermes Agent | Shell hooks, after a one-time approval in Hermes |
| Cloud agents (Claude Code on the web, Codex cloud, CI) | [Remote capture](docs/remote-capture.md) |
| GitHub | The Openshard GitHub App shows receipts on pull requests and runs the merge gate |

Openshard watches these agents. It doesn't run them, slow them down or change how they work. Details: [docs/agent-capture.md](docs/agent-capture.md).

## Free locally. Pay when your team needs it.

| | Free | Pro | Team |
| --- | --- | --- | --- |
| Full receipts on your machine | ✓ | ✓ | ✓ |
| Re-run checks with `openshard verify` | ✓ | ✓ | ✓ |
| Dashboard across all your machines |  | ✓ | ✓ |
| Merge gate |  | Your repos | Your whole org |
| Shared team dashboard, rules and roles |  |  | ✓ |

Prices and trials: [openshard.dev/pricing](https://openshard.dev/pricing). To connect to the dashboard:

```bash
openshard login        # approve this computer in your browser, no key to copy
```

## Your code stays yours

- **On Free, your receipts never leave your machine.** They live in your repo's `.openshard/` folder.
- **When you turn on sync, only facts are sent:** file names, checks, model, tokens and cost. Never your code, prompts, diffs or chat transcripts. See [docs/platform-sync.md](docs/platform-sync.md).
- **Basic usage stats** (version numbers, counts, timings; never code, prompts or file names) help us fix bugs. Turn them off with `openshard telemetry off`. They're always off in CI. See [docs/telemetry.md](docs/telemetry.md).
- **The core is open source** (Apache 2.0), so you can read every line that runs on your machine.

## Everyday commands

```bash
openshard last             # latest receipt (add --more for detail)
openshard history          # recent agent work in this repo
openshard verify           # re-run your checks and record the result
openshard doctor           # is everything set up correctly?
openshard stats            # totals for this repo: agents, models, checks, cost
```

Add `--json` to most commands for scripts. All commands: [docs/cli-reference.md](docs/cli-reference.md).

<details>
<summary><strong>How trustworthy is a receipt? (the honest version)</strong></summary>

<br>

Each receipt is signed with a key stored on your machine. That catches any later edit by someone without the key.

It can't stop the agent itself, because an agent runs as you and can read that key. That's why receipts synced to the dashboard are also witnessed there as the work happens: a record that left your machine can't be quietly rewritten afterwards.

Signed doesn't mean verified. Only an Openshard re-run or a passing CI check on that commit makes a result verified. Full details: [docs/signing.md](docs/signing.md).

</details>

<details>
<summary><strong>Experimental: let Openshard run the task itself</strong></summary>

<br>

Openshard Native (`openshard run`) runs a task itself, choosing the model and enforcing approvals and checks as it goes, instead of only watching another agent. It's early and may change; receipts don't depend on it. See [docs/osn-run.md](docs/osn-run.md).

</details>

## Status

Alpha, shipping most days. Tested on macOS, Linux and Windows on every change.
Found a bug or something odd? [Open an issue](https://github.com/openshard/openshard/issues), or email michael@openshard.dev.
Security issues: please report privately, see [SECURITY.md](SECURITY.md).

[Contributing](CONTRIBUTING.md) · [Architecture](docs/architecture.md) · [Changelog](CHANGELOG.md) · [License](LICENSE)
