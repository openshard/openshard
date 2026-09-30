# Remote capture

A cloud coding task (Codex cloud, Claude Code on the web, a Cursor cloud
agent, a CI job) runs in a VM or container that is destroyed when the task
ends, and sometimes before. Everything OpenShard records locally, including
the Receipt, dies with it.

Remote capture streams the evidence out while the agent works:

```
temporary agent environment
  hook event -> Event (the one OpenShard already builds)
             -> appended to a local spool first (never blocks the hook)
             -> sent in small batches, seconds later, with a short-lived token
             -> acknowledged only once the Platform stored it
permanent hosted account
  remote capture: the journal that survived, linked to the Receipt when one
  is finalised, and honest about how the session ended
```

The environment is temporary. The OpenShard record is permanent. The
Platform runs nothing and owns no model: the agent belongs to whoever runs
it; OpenShard observes what it can, gets it out, and strengthens it later.

## Commands

| Where | Command | What it does |
|---|---|---|
| a machine you trust | `openshard remote create --agent codex\|claude-code\|cursor [--ttl 480]` | opens a hosted remote capture with the Platform link from `openshard sync connect` and prints its token once, with the setup lines for that agent |
| the environment | `openshard remote attach` | reads `OPENSHARD_REMOTE_CAPTURE_URL` / `OPENSHARD_REMOTE_TOKEN` (or `--url` / `--token`), checks them against the Platform and stores the attachment under the OpenShard home (mode 0600) |
| the environment | `openshard remote status`, `openshard remote flush`, `openshard remote detach` | what is attached and queued; send now; forget |
| a machine you trust | `openshard remote list`, `openshard remote revoke <id>` | the organisation's captures; close a token early |

Nothing else changes. Once attached, every hook adapter's Events go to the
spool as they are folded; `openshard verify` and `openshard verify --ci`
ask for their attestation to be delivered; a session's end delivers its
Receipt. The dashboard shows the capture under **Remote**.

## What leaves the environment, and when

Only what the Receipt path already allows: Core's Events
(`history/event.py`) with their scrubbed, bounded action labels, tool
names, repo-relative paths, statuses and evidence sources; the Receipt
identity the session is building; then the ordinary receipt-sync envelope
and verification evidence. Never prompts, transcripts, command output,
environment values, absolute paths or credentials. The spool applies the
Platform's own privacy rules before an Event is written
(`remote/spool.py: wire_event`), so an Event the Platform would refuse is
never sent; anything unsafe is withheld field by field.

Batches leave a few seconds after each burst of hooks: inside the capture
service a flusher thread wakes on every Event; without a service, the hook
starts one detached flusher at a time, at most one per five seconds.
Batches hold up to 100 Events. While idle, an alive flusher sends a
heartbeat once a minute so the Platform can tell "quiet" from "gone".

Measured in the dogfood (a 20-hook session): about 630 bytes of journal per
Event hosted, one batch per burst, and roughly 0.5 s per hook inside the
container, of which the remote spool append is a few milliseconds; the rest
is the hook process itself.

## Failure handling

| Situation | Behaviour |
|---|---|
| Platform unreachable, 429, 5xx | exponential backoff from 5 s to 5 min; the spool keeps growing; everything drains when it is back |
| batch refused (400/422) | resent one Event at a time; only the refused Events are dropped and counted |
| token expired or revoked (401) | sending stops for good, the spool is kept, `remote status` says `unauthorized` |
| journal full (409) | Events stop; the Receipt is still delivered |
| collector restart | the spool and its state are files; the next flush resumes from the last acknowledgement |
| lost acknowledgement | the batch is resent; the Platform de-duplicates on `event_id` |
| two collectors on one capture | both accepted, both shown; no Event is stored twice |
| environment destroyed | whatever was acknowledged is hosted; the capture reads `Partial` once it falls silent, expires or is revoked. No end event and no Receipt are invented |

## Authentication

The environment never holds an organisation API key. `remote create` mints
a capture token (`osr_...`), stored hashed, with a lifetime (default 8 h,
at most 24 h) and revocable from a trusted machine. The token can:

- append Events to its own capture,
- deliver Receipts through its capture (at most 10), and add later
  verification evidence to those Receipts,
- read the status of its own capture.

It cannot read any Receipt, list anything, mint keys, manage members or
policies, or touch another capture or organisation; every such attempt is
the same 401. Assume agent-generated code can read the token: this is the
whole blast radius. The token never appears in the spool, in any Event, in
`remote status`, or in an error.

## How a capture ends

| | Hosted state |
|---|---|
| the session's own end Event arrived (`run.completed`) | **Completed** |
| the end Event was `run.failed` | **Failed** |
| evidence arrived, then nothing for 15 minutes, or the token expired or was revoked | **Partial**, with the reason (`Session ended without a final event`) |
| nothing ever arrived | **Expired** |

A partial capture keeps its journal and the Receipt identity it was told
about. If that Receipt is ever synced by any route, the capture links to
it. A Receipt is never manufactured from a journal.

## Providers (official documentation, checked 2026-09-30)

| | Codex cloud | Claude Code on the web | Cursor cloud agents |
|---|---|---|---|
| setup script with network | yes (install script / start skill) | yes (setup script, runs before Claude Code) | yes (`.cursor/environment.json` install/start) |
| secrets in the agent phase | partial: environment variables are passed to programs; "network secrets" are substituted by a proxy for allowed domains only | partial: environment variables, readable by anyone using the environment | yes: runtime secrets, redacted in transcripts |
| outbound HTTPS to the Platform during the agent phase | needs the host on the allowed domains | needs Custom network access with the host allowed | yes by default |
| repository hooks honoured | **not documented** (cloud orchestration is documented as not running command hooks) | yes, from `.claude/settings.json`, single-repository sessions only | yes, from `.cursor/hooks.json`; `sessionStart` / `sessionEnd` do not fire |
| process may stay alive beside the agent | partial (start skill) | yes, until the VM is reclaimed | yes (tmux terminals) |
| reliable session-end event | no | no (idle reclaim; not documented) | no (`stop` only) |
| observable from outside | diff, PR | branch push, PR, CI | branch push, PR |
| env var identifying a cloud run | not documented | `CLAUDE_CODE_REMOTE=true` | `CURSOR_CODE_REMOTE` |

What this means for each:

- **Claude Code on the web.** Set the two variables in the environment, allow
  the Platform host, run `pip install openshard && openshard remote attach`
  in the setup script, and commit OpenShard's hooks in the repository's
  `.claude/settings.json` (or run `openshard setup --yes` in the setup
  script). Tool calls, file changes, checks and the Receipt stream out; when
  the VM is reclaimed without a `SessionEnd`, the capture ends as Partial
  and a later sync can still reconcile the Receipt.
- **Cursor cloud agents.** Same variables as runtime secrets, `openshard
  remote attach` in `install`, `openshard capture install cursor` committed.
  No session start or end hooks fire in the cloud, so a run reads as
  Partial unless its Receipt is delivered; tool and file evidence still
  streams.
- **Codex cloud.** Repository hooks in cloud tasks are not documented, so
  tool-level capture may not be available. What is observable is the
  branch, the PR and its CI: run `openshard verify --ci` against the
  resulting commit from a trusted checkout. A capture can still be opened
  to hold whatever a setup script chooses to run.

None of these was exercised against a live cloud session in this release;
the survival and reconciliation proofs ran in disposable containers with
the same hooks and the same collector. Treat the table as what the
providers document, not as verified behaviour.

## Fallback: Git and CI only

Where OpenShard cannot run, be installed, reach the network or install
hooks, nothing about the session is observable. What remains is what the
agent pushes: a branch, a pull request, CI. From a trusted checkout,
`openshard verify --ci` attaches the CI verdict for that exact commit to a
Receipt it has locally; attaching it to a hosted Receipt that only exists
on the Platform is not yet possible.
