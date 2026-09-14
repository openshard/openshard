# Security Policy

## Reporting a vulnerability

Please do not open a public GitHub issue for a security problem. Report it
privately by emailing **michaelobasa2@gmail.com** with a description, steps
to reproduce, and what you think the impact is. You will get a response as
quickly as possible and we will agree a fix and disclosure timeline with
you before anything is published.

## What OpenShard is, in security terms

OpenShard records evidence about coding-agent sessions into a local file
(`.openshard/runs.jsonl`) and renders it. It does not run your agent, does
not proxy model traffic, and needs no account. The components that matter
for security are below, with what each one trusts.

### Local capture service (`openshard capture ...`)

* Listens on `127.0.0.1` only, one instance per user, started on demand
  by the hook entrypoints and exiting after four idle hours.
* **Authentication (v0.4.4).** Every `POST` must carry
  `X-OpenShard-Capture-Token`: either the per-user capture token
  (`~/.openshard/capture-token`, created locally, mode 0600) or the
  repository-scoped capability derived from it. A request without a valid
  credential is refused before its body is parsed and nothing is
  recorded; a request carrying browser headers (`Origin`, `Referer`,
  `Sec-Fetch-*`) is refused outright. `POST /shutdown` accepts the token
  only, never a repository capability. `GET /health` is the only
  unauthenticated endpoint and returns counters and an informational
  instance id; it authorises nothing.
* What the service stores outside `runs.jsonl`: per-session queue lines
  containing only the *reduced* payload (scrubbed 300-character prompt
  excerpt, repo-relative paths, a 100-character summarized command), and,
  since v0.4.4, bounded copies of queue lines that could not be decoded
  under `.openshard/claude_sessions/quarantine/`. Never transcripts, tool
  output, file contents, environment variables or absolute paths.
* `openshard capture rotate-token` replaces the token; re-run
  `openshard setup` in each repository that captures Claude Code.

### Hooks and plugins

* Claude Code: HTTP hooks in `.claude/settings.local.json` carry the
  repository-scoped capability (that file is added to
  `.git/info/exclude` by the installer; the installer refuses to write a
  credential into it when git tracks it). The `SessionStart` command hook
  and the status line run `openshard` and read the token file.
* Codex and Cursor: command hooks run `openshard hooks codex|cursor`,
  which reads the token file.
* OpenCode: the plugin at `.opencode/plugins/openshard.ts` reads the same
  token file at delivery time.
* All of them are **fail-open for the agent**: if OpenShard is missing,
  refuses, or times out, the coding agent continues; only evidence is
  lost, and the service counts refusals.

### MCP server (`openshard mcp ...`)

Read-only over this repository's history; exposes `recent_shards`,
`get_shard`, `get_receipt`, `search_history`, `relevant_context`. It
never returns raw prompts, transcripts or file contents, only the same
bounded, sanitised fields the CLI renders.

### Telemetry

Optional, off in CI and under `DO_NOT_TRACK`. The schema has no free-text
property, so the capture token, paths, prompts, repository names and
receipt contents cannot be sent even by mistake. See `docs/telemetry.md`.

### What a Receipt does and does not prove

* `Integrity  Matches (content hash)` means the stored record equals what
  was hashed when it was written. The hash is unkeyed SHA-256: tamper
  evidence for the file, **not** a signature and not proof of authorship.
* `Changed N files` counts agent-reported and git-observed changes; files
  that were already dirty before the session or that another live agent
  session reported are excluded and listed separately. Git-observed means
  the repository changed; it does not establish who changed it.
* `Capture  Incomplete — …` means OpenShard knows evidence was lost; a
  receipt without that line is not a claim that nothing was missed, only
  that no loss was detected.

## Scope

In scope: the `openshard` package (CLI, capture service, hooks and
plugins, MCP server, history and receipt rendering, telemetry client).
Out of scope: the coding agents themselves and third-party model
providers; report those to their vendors.
