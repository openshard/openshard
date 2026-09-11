# Telemetry ("Help improve OpenShard")

OpenShard can share a small amount of anonymous usage and reliability data
so we can tell whether it works, which agents people use it with, and where
it fails. This page is the complete contract: what is collected, what is
never collected, how to see it, and how to turn it off.

The short version:

- Only counts, versions, timings and fixed category values are sent. The
  schema has no free-text field, so code, prompts, file names, repository
  names, secrets and receipt contents cannot be sent even by mistake.
- It is on after you have seen the notice (`openshard setup` or the
  onboarding flow) and off the moment you say so: `openshard telemetry off`.
- `openshard telemetry sample` shows you the exact events waiting to leave
  your machine, verbatim.

## What is collected

Every event is an envelope of fixed keys:

| Key | Value |
| --- | --- |
| `schema_version` | `1` |
| `event_id` | random uuid4 |
| `event_type` | one of the names below |
| `occurred_at` | UTC timestamp, whole seconds |
| `installation_id` | random uuid4 minted locally (see below) |
| `consent_level` | `improve` |
| `openshard_version` | e.g. `0.4.2` |
| `platform` | `os` (`windows`/`linux`/`darwin`/`other`), `arch` (`x86_64`/`arm64`/`other`), `python` (`3.12`) |
| `properties` | the event's own fields, listed below |

There is no hostname, username, locale, timezone, IP-derived field or precise
timestamp in the envelope.

The event types and every property each can carry (`openshard/telemetry/schema.py`
is the source of truth):

| Event | Properties |
| --- | --- |
| `install.seen` | `first_run` (bool) |
| `setup.completed` | `agents` (list from `claude_code`/`codex`/`opencode`/`cursor`), `mcp` (bool), `capture_service` (`ok`/`failed`/`disabled`), `result`, `error_category` |
| `command.invoked` | `command` (a fixed list: `setup`, `doctor`, `last`, `history`, `context`, `stats`, `capture.*`, `mcp.*`, `telemetry.*`, ...), `duration_ms`, `result` (`ok`/`error`), `error_category` |
| `receipt.created`, `receipt.completed` | `agent`, `origin` (`openshard_routed`/`external_observed`/`unknown`), `capture_depth` (`full`/`partial`/`unknown`), `files_changed` (count), `files_source` (`git_diff`/`hook_reported`/`not_available`/`other`), `tool_calls`, `tool_failures`, `checks` (`none`/`attempted_unverified`/`passed`/`failed`), `attempt_number`, `is_retry`, `turn_count`, `duration_s`, `cost_usd` (2 decimals), `model_family` |
| `history.queried` | `command` (`history`/`context`/`search`/`relevant_context`/`last`/`stats`), `results` (count), `duration_ms` |
| `mcp.tool_called` | `tool` (`recent_shards`/`get_shard`/`get_receipt`/`search_history`/`relevant_context`), `results`, `duration_ms`, `result` |
| `capture.service` | `state` (`started`/`stopped`/`idle_exit`/`spawn_failed`), `queued`, `folded`, `replay_errors`, `p50_ms`, `p95_ms` |
| `error.occurred` | `component` (`cli`/`hooks`/`capture_service`/`mcp`/`native_run`), `category` |
| `telemetry.consent_changed` | `improve` (`on`), `source` (`setup`/`onboarding`/`cli`/`env`/`config`) |

`error_category` and `category` are always one of `timeout`, `permission`,
`io`, `parse`, `git_unavailable`, `lock_timeout`, `usage`, `unknown` -- never
an error message.

`model_family` is a public family name (`claude`, `gpt`, `o-series`, `codex`,
`gemini`, `llama`, `mistral`, `deepseek`, `qwen`, `grok`, `unknown`, `other`),
never the model slug, so a private or custom model name cannot identify an
organisation.

## What is never collected

By construction, not by policy: every property is validated against the
schema above before it is queued. A string property may only be a member of
its fixed list, or (for the version) a bounded token of letters, digits,
dots, dashes and underscores that is additionally rejected if it looks like
a secret. That grammar cannot express a path, an email address, a URL, a
repository name, a shell command or a prompt. An unknown property is dropped;
an invalid property drops the whole event.

So the following are never sent:

- source code, diffs or file contents
- prompts, agent responses, transcripts or notes
- file names, directory names or paths
- repository names, git remotes, branch names or commit hashes
- model slugs (only the public family above)
- API keys, tokens or anything matching OpenShard's secret patterns
- receipt or Shard contents, task text, error messages
- hostname, username, email, IP address, locale or timezone

`openshard telemetry sample` prints the queued events exactly as they will
be sent, so you can check this yourself at any time.

## Installation id

`installation_id` is a random uuid4 minted the first time telemetry state is
created. It is stored user-globally in `telemetry.json` under your OpenShard
home (`~/.openshard`, or `OPENSHARD_HOME`), never inside a repository, and it
is never derived from a username, hostname, email, MAC address or repository
path. `openshard telemetry reset` mints a new one and keeps your consent
choice.

## Default behaviour and consent

Consent has three states, kept in the same `telemetry.json`:

- `unset` -- you have not seen the notice yet. Nothing is sent. An install
  that predates telemetry stays here, silent, until you next run
  `openshard setup` or the onboarding flow.
- `on` -- you saw the "Help improve OpenShard" notice in `openshard setup`
  or the onboarding flow and continued past it, or ran
  `openshard telemetry on`. Events are queued and sent.
- `off` -- you ran `openshard telemetry off`. Nothing is queued or sent, and
  anything still queued is discarded.

Seeing the notice turns an `unset` consent on; it never overrides a decision
already made. `openshard setup --json` and `openshard setup --agent` are read
by machines and never decide for a person, so they leave consent `unset`.

### Turning it off

Any one of these disables telemetry, and none of them can enable it:

| Method | Scope |
| --- | --- |
| `openshard telemetry off` | this user, persistently |
| `OPENSHARD_TELEMETRY=off` (also `0`, `false`, `no`, `disabled`) | this environment |
| `DO_NOT_TRACK=1` (the cross-tool convention) | this environment |
| `CI`, `GITHUB_ACTIONS` or `GITLAB_CI` set | CI runs are never counted |
| `telemetry: {enabled: false}` in a repository's `.openshard/config.yml` | everyone working in that repository |

`openshard telemetry status` shows the effective state and which of these
rules, if any, is turning it off.

## How events leave the machine

- `emit` never blocks, never prints and never raises. When telemetry is off
  it returns after an environment/consent check. When on, the event is
  validated and appended to a local queue file (`telemetry.queue.jsonl` in
  your OpenShard home).
- The queue is bounded: at most 500 events or 256 KiB, dropping the oldest
  first. Telemetry is not evidence; loss is acceptable, growth is not.
- Sending is a single HTTPS `POST` of a JSON batch (at most 50 events) to
  the endpoint shown by `openshard telemetry status`, with a 3-second total
  timeout. The CLI flushes from a background thread it never waits for; the
  capture service flushes once a minute. Plain `http://` is refused except to
  loopback, so nothing is sent unencrypted over a network.
- On a failed send the batch is put back at the front of the queue and
  sending backs off exponentially: 1 minute after the first failure,
  doubling to a maximum of 1 hour. A `4xx` response means the server rejected
  the batch; it is dropped rather than retried forever.
- Nothing telemetry-related ever runs on a coding agent's hook path. Hooks
  are handled by the capture service, which emits after a fold on its own
  worker thread.

The endpoint can be overridden with `OPENSHARD_TELEMETRY_ENDPOINT` or
`telemetry: {endpoint: ...}` in `.openshard/config.yml` (HTTPS only, or HTTP
to loopback for local testing). An unset or invalid endpoint means nothing is
sent.

## Commands

```
openshard telemetry status [--json]   effective state, consent, installation id, endpoint, queue size
openshard telemetry on                turn on for this user
openshard telemetry off               turn off for this user and discard the queue
openshard telemetry reset             mint a new installation id (consent unchanged)
openshard telemetry sample [--limit]  print the queued events verbatim
```

`openshard setup` and `openshard doctor` also show the current state on one
line.

## Changing the schema

Adding an event type or property is a deliberate change: add it to
`openshard/telemetry/schema.py`, bump `SCHEMA_VERSION` if the envelope
changes, and update the tables on this page in the same change. The names
under `RESERVED_EVENT_TYPES` (`attempt.outcome`, `context.retrieval.outcome`,
`developer.correction`, `agent.handoff`) belong to a future, separate,
off-by-default "richer development data" consent and are rejected by the
current schema.
