# Zero-setup cloud capture (design)

Status: proposal, post-v0.5.0. Provider facts are from official docs,
untested in real VMs.

## Plain summary

- Once, on app.openshard.dev: **Connect GitHub**, pick repositories.
- Openshard opens one **Add Openshard** pull request per repository. Merge it.
- Any cloud agent on that repository then produces a Receipt: what Openshard
  observed in the VM where it can run there, otherwise what GitHub shows,
  labelled as such.
- The hard part is proving *who sent* the evidence without anyone pasting a
  key. Only GitHub-run agents (Copilot) get that for free. Elsewhere we either
  ask for one paste per platform, or accept evidence from an unauthenticated
  sender and label it that way.
- Matching evidence to a real commit proves the commit exists in the
  repository. It does not prove the evidence describes how that commit was made.

## User flow

1. **Connect GitHub** (Settings -> Repositories). Install the Openshard GitHub
   App on chosen repositories. Permissions: metadata, contents and pull
   requests (write, for the PR branch), checks (read), and workflows (write),
   needed for any file under `.github/workflows/`, including Copilot's.
2. **Repositories** screen: one row per repository, status
   `PR open / Connected / Seen on GitHub only / Capture verified (<agent>)`.
3. **Add Openshard PR**: the files below on one branch; the body lists every
   file, what each agent will run, and any key it contains (option A only).
4. **Agents** screen: per platform, either "Ready", or "One step left: add this
   secret in <platform>" with a copy button.
5. A row reads `Capture verified (<agent>)` only after a real Event from that
   agent arrives (configured is not captured).

## What the PR writes

Every command is guarded (`command -v openshard || exit 0`) so a
developer's local agent without Openshard is unaffected. Setup pins an exact
Core version (`pip install openshard==X.Y.Z`), never latest.

| Platform | Files | Hooks that fire (research) | Result |
|---|---|---|---|
| Claude Code cloud | `.claude/settings.json` command hooks; setup line `pip install openshard==X && openshard capture install claude --defer-service` shown for the environment UI | repo hooks apply; no session-end on VM reclaim | in-VM, per-turn Receipts at `Stop` (exists for `claude-code-web`) |
| Copilot cloud agent | `.github/workflows/copilot-setup-steps.yml` (one job `copilot-setup-steps`); `.github/hooks/openshard.json` | sessionStart, sessionEnd, agentStop, tool hooks: best instrumented | in-VM; needs a new Copilot adapter |
| Cursor cloud agents | `.cursor/environment.json` install; `.cursor/hooks.json` | project hooks run; staff said lifecycle hooks now fire (Jul 2026); Core's note says they do not, stale until tested | in-VM, per-turn at `stop` (exists for `cursor-cloud`) |
| OpenHands | `.openhands/setup.sh`, `.openhands/hooks.json` | Stop documented | in-VM; new adapter |
| Devin | `.devin/blueprint.yaml` maintenance | no hooks documented | GitHub-observed; secrets removed before snapshot |
| Amp Orbs | `.agents/setup`, `.agents/resume` | hooks unknown | GitHub-observed until tested |
| Replicas | `replicas.json` startHook | runs Claude Code/Codex, whose repo hooks apply | as Claude Code; Codex GitHub-only |
| Codespaces / devcontainers | devcontainer feature | the agent's own hooks | as that agent |
| Codex cloud, Jules, Boat | none in repo (setup lives in their UI) | Codex cloud hooks undocumented; Jules unknown; Boat runs Claude Code/Codex | GitHub-observed; Boat with Claude Code as Claude Code once its setup runs the line |
| Bolt, Lovable, v0, Replit, Grok | none | none | GitHub-observed |

GitHub-observed Receipts come from **App webhooks** (push, pull_request,
check_run), not a per-repo workflow: no Actions minutes or extra
permission. The OIDC workflow stays optional.

## Authentication

Every in-VM credential is assumed readable by agent code. All options reuse
connected capture (journals created on first contact); none adds an ingest path.

| Option | How | Zero paste? | Forged Receipts | Spam / cost inflation | Leakage |
|---|---|---|---|---|---|
| **A. Committed send-only key** (`osi_`, repo-bound) | in repo config | yes | anyone who can read the repo can send anything; public repo = the world | rate limit per key, journal cap; forged usage could inflate cost | public by design; rotation needs a PR |
| **B. Provider proxy secret** (Claude network secrets, Codex vault) | provider injects `osc_`, never in VM | no: one paste per environment | only code in that environment | bounded to that environment | low; exists today |
| **C. GitHub OIDC** (Actions, Copilot setup steps) | setup step exchanges OIDC for a short-lived repo-bound `osr_` | yes | only workflows of this repo at a known path | bounded | token lives in VM for its TTL |
| **D. App-set secrets** | App writes a secret via platform API (GitHub `copilot` environment secrets; others only if an API exists) | yes where an API exists | only that environment | bounded | secret in VM env |
| **E. Device-flow claim** | VM prints a code; a member approves | no: a human per environment | only approved environments | bounded | low |

Fork trap for A and matching: GitHub serves commits from any fork in the
network through the parent's API. "Commit exists" must mean reachable from a
branch or PR of this repository, checked by the App, not a commit lookup.

**Privacy**: all options pass the existing privacy guard; A can still carry
an attacker's strings, so it renders as untrusted text.

**Recommendation**: C for Copilot and Actions; B for Claude Code and Codex
(one paste, already built); D wherever a platform API allows. A only as a
founder-approved fallback for private repositories, off by default for public
ones, its evidence never counted in Insights totals or cost. Option A also
needs a deliberate Core rule change: installers today refuse to write any
credential into a tracked file.

## What each Receipt must say

| Provenance | Receipt wording |
|---|---|
| In-VM, authenticated sender (B, C, D, E) | "Observed by Openshard in <surface> · connected by <account>"; `Partial: session ended without a final event` when no end arrives |
| In-VM, committed key (A), commit matched | "Sent from an environment using this repository's shared key; sender not authenticated. Refers to commit abc, which GitHub shows on <branch>" |
| In-VM, committed key, not matched | "Unverified: sender not authenticated and commit not found in this repository". Hidden from default views and Insights |
| GitHub-observed | "Seen on GitHub: commit abc by <account>. Agent identified from <trailer / account role>. Runtime not observed; model, tokens and cost unknown" |
| CI on that commit | "CI for commit abc reported by GitHub checks" (or "re-run by Openshard's workflow" for OIDC) |

A match is never "verified": it ties evidence to a commit, not to the run.
A member's "This was me" adds "claimed by <member>", no higher level.

## Exists vs to build

| | Exists | To build |
|---|---|---|
| Core | connected capture, proxy mode, remote spool, per-turn seal (Claude, Cursor cloud), Claude/Cursor/Codex adapters, `usage trailers` | Copilot and OpenHands adapters; guarded committed-hook mode; OIDC exchange in `copilot-setup-steps`; option A credential if chosen; fix stale Cursor note in `remote create` |
| Platform | `osc_` connections, remote captures + witness journal, OIDC route (env allowlist, one org), agent-account allowlist, device flow | GitHub App (install, per-installation repo -> org mapping replacing the env allowlist), PR generator, webhooks -> GitHub-observed Receipts, branch-reachability match, OIDC -> `osr_` exchange, repo-bound credential kind, Repositories and Agents screens, unverified quarantine |

## Phases

- **v0.5.0**: nothing new. Ship the manual paths (Claude proxy secret, Cursor
  runtime secret, allowlisted OIDC workflow); fix the stale Cursor note once a
  real run proves it.
- **Phase 1**: GitHub App, webhooks, GitHub-observed Receipts, Add Openshard
  PR for Claude Code, Copilot (C) and Cursor; Repositories screen.
- **Phase 2**: D where APIs allow; OpenHands, Devin, Amp, Replicas files;
  founder decision on A.
- **Phase 3**: remaining platforms as their hooks are proven.

## E2E test plan

Two test repositories (private, public) with the App installed. Nightly,
real VMs, Claude Code cloud, Copilot and Cursor first:

1. Start one small task per platform (assign an issue to Copilot; Cursor
   Cloud Agents API; Claude Code cloud by any programmatic entry, else a
   scheduled manual run, reported as manual).
2. Assert within 10 minutes: a capture for the right repository and surface;
   the expected hook Event types; one sealed Receipt per completed turn;
   `Partial` with reason when no end arrives; the pushed commit matched via
   the App.
3. Negative tests: evidence for an unknown commit and for a fork-only commit
   stays unverified; a revoked credential gets 401; a sender over the rate
   limit gets 429; no prompt or command output is stored.
4. Record which hooks fired; a platform is listed as in-VM only after two
   consecutive green nights.

## Founder decisions

1. Allow option A at all; if so, private-only or also public.
2. Accept one paste per platform (B) where no API exists.
3. Ask for the workflows permission, or skip Copilot in-VM capture.
4. Which platforms enter Phase 1.
