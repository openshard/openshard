<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/openshard/openshard/main/docs/assets/openshard-wordmark-white.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/openshard/openshard/main/docs/assets/openshard-wordmark-black.png">
    <img src="https://raw.githubusercontent.com/openshard/openshard/main/docs/assets/openshard-wordmark-black.png" alt="Openshard" width="700">
  </picture>
</p>

<p align="center">
  <strong>Receipts for AI coding agents.</strong>
</p>

<p align="center">
  Proof of what every agent change did, whether it was independently verified, and what it cost, before it merges.
</p>

<p align="center">
  <a href="https://github.com/openshard/openshard/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg?style=for-the-badge" alt="License"></a>
  <a href="https://pypi.org/project/openshard/"><img src="https://img.shields.io/pypi/v/openshard?style=for-the-badge&label=PyPI" alt="PyPI"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/status-alpha-orange?style=for-the-badge" alt="Status">
</p>

Keep using Claude Code, Codex, Cursor or another supported agent the way you do now. When it finishes, Openshard gives you a receipt: what ran, what changed, whether the checks were re-run by something other than the agent, what it cost, and what could not be established.

## Quickstart

```bash
pip install openshard        # or: pipx install openshard / uv tool install openshard
cd your-project
openshard setup              # detects your agents and installs capture
# ...use your coding agent as normal...
openshard last               # the receipt for the latest run
```

`openshard doctor` shows which agents are set up and what can be captured. Needs Python 3.11+ and git; for macOS, Windows, Linux and cloud/VM sandboxes (a one-line setup script) see [docs/install.md](docs/install.md).

## What a receipt shows

- **What changed.** Files the agent reported changing, kept apart from changes Git only observed, changes that were already there, and changes from another session. If the actor can't be established, the receipt says so.
- **Whether it was verified.** An agent saying "tests pass" is recorded as agent-reported, not verified. `openshard verify` re-runs your checks and records the result Openshard observed; `openshard verify --ci` attaches the GitHub check result for the exact commit.
- **What it cost.** Model, tokens and estimated cost when the agent exposes them; `not recorded` when it doesn't.
- **What's missing.** Unknown stays `unknown`. A partial capture is marked partial. A finished turn is `Turn completed (unverified)` until something checks it.

For CI and scripts, `openshard verdict --commit <sha>` answers "is this verified?" from recorded evidence (exit 0 verified, 1 failed, 2 unknown). See [verdict](https://github.com/openshard/openshard/blob/main/docs/verdict.md).

<!-- MERGE GATE (new): keep this section only if the merge-gate PR (docs/merge-gate.md, root action.yml) has merged. Otherwise delete it. -->
## Merge gate (new)

A GitHub Action that reads the head commit's receipt from the Openshard Platform and passes or fails the pull request: verified passes; failed, unfinished, or agent work with no receipt fails. Mark "Openshard gate" as a required check to stop that work from merging. Setup and exact behaviour: [docs/merge-gate.md](https://github.com/openshard/openshard/blob/main/docs/merge-gate.md).
<!-- /MERGE GATE -->

## Supported agents

| Agent | Capture |
| --- | --- |
| Claude Code, Codex, Cursor, OpenCode | Hooks/plugins, set up by `openshard setup` |
| Google Antigravity, Grok Build | Native hooks |
| Hermes Agent | Shell hooks (`openshard capture install hermes`) |
| Grok Bot (Cursor cloud) | Enterprise: Cursor OpenTelemetry export. Other plans: self-report skill (agent-reported only). See [Grok Bot](https://github.com/openshard/openshard/blob/main/docs/grok-bot.md) |
| Cloud agents (Codex cloud, Claude Code on the web, CI) | [Remote capture](https://github.com/openshard/openshard/blob/main/docs/remote-capture.md) streams evidence out before the environment is destroyed |

Openshard observes these agents. It does not run, sandbox or block them. Details: [Agent capture](https://github.com/openshard/openshard/blob/main/docs/agent-capture.md).

## How trustworthy is a receipt?

Each receipt is sealed with an Ed25519 key in `~/.openshard/keys/` on the machine the agent runs on. That catches later edits by anyone without the key. It does not stop the agent itself: an agent running as your OS user can read that key and seal a forged record.

A receipt is only tamper-evident against the agent when its events were witnessed by the Openshard Platform: sent off the machine as they happened, with the Platform recording when they arrived. Witnessed still isn't verified; only a re-run or CI makes a result verified. See [Signed Receipts and the hosted witness](https://github.com/openshard/openshard/blob/main/docs/signing.md).

## Local first, hosted optional

Receipts live in your repo (`.openshard/`) and work offline with no account. To see them across machines and your team at [app.openshard.dev](https://app.openshard.dev):

```bash
openshard login       # approve this computer in the browser
openshard sync now
```

Sync sends only the bounded fields `openshard history --json` prints: no prompts, transcripts, diffs or file contents. See [Platform sync](https://github.com/openshard/openshard/blob/main/docs/platform-sync.md).

Privacy-safe usage telemetry (versions, counts, timings, error categories; never code, prompts, file names or receipt contents) can be turned off with `openshard telemetry off`. See [Telemetry](https://github.com/openshard/openshard/blob/main/docs/telemetry.md).

## Everyday commands

```bash
openshard last [--more | --full]   # latest receipt
openshard history                  # recent agent work in this repo
openshard verify                   # re-run checks for the latest receipt
openshard verify --signature       # check a receipt's seal offline
openshard stats                    # repo-level counts
```

Most commands take `--json`. Full list: [CLI reference](https://github.com/openshard/openshard/blob/main/docs/cli-reference.md).

## Advanced / experimental

These live beside the receipt path. Receipts don't depend on them, and they may change.

- **Openshard Native (OSN):** Openshard runs a task itself (`openshard run`, `openshard osn`) with sandboxing, approvals and verification it actually controls. See [OSN](https://github.com/openshard/openshard/blob/main/docs/osn-run.md).
- **Model routing** for native runs. See [Routing](https://github.com/openshard/openshard/blob/main/docs/architecture/routing.md).
- **Learning** from verified past runs. See [Learning](https://github.com/openshard/openshard/blob/main/docs/learning.md).
- **Evals** (`openshard eval`) and **Trust Score** (`openshard trust`, a diagnostic heuristic, not a receipt signal).

## Status and links

Alpha, Apache-2.0, built in the open. CI runs Ruff, mypy and pytest on Linux, macOS and Windows.

[Docs](https://github.com/openshard/openshard/tree/main/docs) ·
[Architecture](https://github.com/openshard/openshard/blob/main/docs/architecture.md) ·
[Changelog](https://github.com/openshard/openshard/blob/main/CHANGELOG.md) ·
[Contributing](https://github.com/openshard/openshard/blob/main/CONTRIBUTING.md) ·
[Security](https://github.com/openshard/openshard/blob/main/SECURITY.md) (report issues privately)
