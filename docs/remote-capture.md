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

## Providers (official documentation, checked 2026-10-06)

The cloud runtimes do not expose identical evidence. Openshard uses the
provider-native hook/runtime path that actually exists and leaves unavailable
fields unknown.

| | Codex Cloud / ChatGPT Work coding runtime | Claude Code on the web | Cursor Cloud Agents |
|---|---|---|---|
| reusable cloud setup | yes: published Codex Cloud environments | yes | yes |
| repository lifecycle hooks | yes in the Codex runtime, including ChatGPT Work, when the hook scripts exist in the execution environment and the user trusts them; ordinary Chat does **not** run these hooks | yes | yes: project command hooks; cloud/background agents may omit session start/end |
| secret safe from agent code | OpenAI-hosted vault environment credential: sandbox sees an opaque placeholder and the network proxy supplies the real secret only to approved hosts | API Credentials host-scoped proxy | Cursor Runtime Secrets are redacted from agent outputs/transcripts but still exist as environment variables inside the VM |
| outbound Platform access | allow `api.openshard.dev` in the environment network policy | allow the Platform host | allow the Platform host |
| model evidence | hook `model`; matching runtime transcript can also name provider/model | hook/status/transcript | hook `model` / `model_id` |
| token/cost evidence | matching runtime transcript cumulative `token_count`; list-rate estimate only when one known priced model served the aggregate | transcript usage plus Claude Code estimate / list-rate fallback | hooks expose neither; official Cursor usage can be reconciled later when a strong run id matches |
| session-end reliability | use the lifecycle hook when delivered; destroyed/aborted environments can still leave partial capture | cloud VM reclaim may leave a partial session | cloud/background agents may omit `sessionEnd`; partial is honest |

Current official references:
- OpenAI Codex Cloud environments: reusable prepared environments can be used from desktop, web and mobile.
- OpenAI plugin/hook docs: lifecycle hooks run in the Codex runtime, including
  ChatGPT Work and Codex; hook scripts must exist in the execution environment
  and be trusted. Ordinary Chat does not run these handlers.
- OpenAI sandbox/vault docs: an environment-variable credential gives sandbox
  code only a placeholder; a network proxy replaces it for approved HTTPS hosts.
- Cursor Cloud Agents docs: project command hooks run in cloud workspaces;
  Runtime Secrets are redacted from agent-visible outputs but exist in the VM.

What this means:

- **Claude Code on the web.** Use the persistent connected-capture flow below.
  Once the environment is configured, normal Claude work streams events and
  the completed Receipt automatically. If the host never delivers a final
  lifecycle event, Openshard keeps the evidence and labels the capture partial.
- **Codex Cloud / ChatGPT Work coding tasks.** Install Openshard and its Codex
  project hooks in the published environment, trust the hooks, and use an
  OpenAI-hosted vault environment credential named
  `OPENSHARD_CONNECTED_TOKEN`, scoped to `api.openshard.dev`. Set
  `OPENSHARD_CONNECTED_CREDENTIAL_MODE=proxy`; the vault's opaque placeholder
  is sent unchanged and OpenAI's proxy supplies the real `osc_` credential.
  Current-task transcript usage is read only after the transcript proves the
  same Codex session id. Ordinary Chat is not claimed as passive capture.
- **Cursor Cloud Agents.** Install project hooks in the cloud environment and
  store `OPENSHARD_CONNECTED_TOKEN` as a Cursor Runtime Secret. The hook stream
  creates the Receipt; later official Cursor usage evidence can strengthen that
  same Receipt when its run identity matches.

These provider capabilities still need live dogfood before Openshard may claim
the complete stranger journey as proven. The table describes the current
provider contracts, not proof that our end-to-end integration has passed.

## Claude Cloud: a persistent connection without the secret in the environment

A connected-capture credential (`osc_...`, created once under **Settings ->
Agents -> Claude Cloud**) lets every Claude Code on the web session stream
without a per-run `remote create`. Claude Cloud can hold that secret in its
**API Credentials** store and inject it, through its egress proxy, into
requests to the Platform host only. Set it up so that the token never sits
in an environment variable or a file:

1. In Claude Cloud **API Credentials**, store the real `osc_` token for
   `api.openshard.dev` (the Platform host the connection was created on).
2. In the environment, set the non-secret marker and the other three values the
   Agents page shows:

   ```
   OPENSHARD_CONNECTED_TOKEN=proxy-injected
   OPENSHARD_CONNECTED_ENDPOINT=https://api.openshard.dev
   OPENSHARD_CONNECTED_ORG_ID=<organisation id>
   OPENSHARD_CONNECTED_SURFACE=claude-code-web
   ```

3. Allow outbound HTTPS to the Platform host and run the usual setup script
   (`pip install -U openshard && openshard setup --yes`).

OpenShard sends `Authorization: Bearer proxy-injected` through the same
transport as any other token and the proxy replaces it. OpenShard cannot see
whether that happened: if nothing is injected, the Platform answers 401 and
the collector stops with `unauthorized`, exactly as for an expired token
(see **Failure handling**); if a credential of the wrong kind is injected,
the Platform answers 403 and nothing is accepted. `remote status` reports
the connection source as `proxy`. The fixed marker is the Claude path. Providers whose secure vault exposes an
opaque placeholder can instead set
`OPENSHARD_CONNECTED_CREDENTIAL_MODE=proxy`; proxy mode deliberately rejects
a real `osc_`/`osk_` value so an accidentally exposed secret fails closed.
Do not put the real token in a normal environment variable or file: agent
generated code can read them.

## Fallback: Git and CI only

Where OpenShard cannot run, be installed, reach the network or install
hooks, nothing about the session is observable. What remains is what the
agent pushes: a branch, a pull request, CI. From a trusted checkout,
`openshard verify --ci` attaches the CI verdict for that exact commit to a
Receipt it has locally; attaching it to a hosted Receipt that only exists
on the Platform is not yet possible.
