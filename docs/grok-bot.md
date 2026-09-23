# Grok Bot (Cursor)

Grok Bot is Cursor's always-on cloud teammate. It works on a persistent
**cloud** computer, so the local mechanisms OpenShard uses for Claude
Code, Codex, Cursor IDE, OpenCode and Antigravity (agent hooks on this
machine, `git diff` of this repository) do not apply. This page covers
Grok Bot only. It is **not** Grok Build.

OpenShard supports two capture paths. They produce different evidence,
and the receipts label them differently.

| | Enterprise: Action Recording | Every plan: self-report |
|---|---|---|
| Who records the facts | Cursor's platform (Action Recording) | The Bot itself |
| Transport | Cursor OpenTelemetry Export -> your collector -> `openshard grok-bot ingest` / `serve` | Bot runs `openshard grok-bot report` on your desktop (Execution on Local Computer) |
| Event evidence | `directly_observed`, `metadata.observer = cursor_action_recording` | `agent_reported` |
| `capture.evidence_level` | `platform_observed` | `agent_reported` |
| Executor / label | `grok_bot_otel` / "Grok Bot (external)" | `grok_bot_report` / "Grok Bot (external)" |
| Capture depth | `partial` | `partial` |
| Completeness | `complete` unless events were dropped | always `incomplete` (`integration_limitation`: anything the Bot didn't report is missing) |
| Task text | not exported (placeholder) | as the Bot states it |
| Shell commands | every command, secret-scrubbed by Cursor, re-scrubbed here; exit code **not** exported | only what the Bot reports |
| Policy blocks | Cursor's shell-policy denials (`approval.denied`) | none |
| MCP tool calls | server, tool, success/failure, transport, duration | only what the Bot reports |
| Browser | host of each navigation (path, query and page title dropped) | only what the Bot reports |
| Computer use | action / screenshot counts and duration | only what the Bot reports |
| Files changed | **not observable** | the Bot's claim |
| Checks | invoked, outcome **unknown** | the Bot's claim, labelled "(agent claim, not observed)" |
| Tokens / model | from `api_request` logs (`tokens_provenance = vendor_telemetry`) | model name if the Bot states it |
| Cost | not recorded (Cursor exports cost only as a metric) | not recorded |
| Conversation end | not exported | the report itself |

Neither path makes OpenShard the observer. For the Enterprise path, Cursor
observed the actions and OpenShard relays them. For the self-report path,
nothing was observed. Receiving an HTTP request or CLI call is not
observation.

## Enterprise: Action Recording -> OpenTelemetry -> Shard

Sources:
[Grok Bot for Teams and Enterprise](https://cursor.com/docs/grok-bot/teams),
[Grok Bot security](https://cursor.com/docs/grok-bot/security),
[OpenTelemetry Export](https://cursor.com/docs/enterprise/opentelemetry-export),
[Wire reference](https://cursor.com/docs/enterprise/opentelemetry-export/wire).

### What Cursor sends

* **Action Recording** (Enterprise only, off by default, a setting on the
  Grok Bot admin page) records MCP tool calls, shell commands, browser
  navigations and computer-use sessions. Privacy Mode (Legacy) forces it
  off. These events do not appear on the Audit Log page.
* **OpenTelemetry Export** (Enterprise only, Team Settings) pushes
  **OTLP/HTTP protobuf** to `<base>/v1/logs` and `<base>/v1/metrics` on an
  **HTTPS endpoint reachable from the public internet**, with a static auth
  header you configure. gRPC and JSON are not offered. Source IPs are
  published on the export page.
* Resource attributes: `service.name=cursor`, `cursor.team.id`,
  `cursor.surface` (`grok_bot` for these events), `cursor.entrypoint`,
  optional `cursor.user.id`.
* The event name is the constant log body: `grok_bot_shell_command`,
  `grok_bot_mcp_tool_call`, `grok_bot_browser_navigation`,
  `grok_bot_computer_use_session`, plus the shared `api_request` /
  `api_error`.
* `cursor.event.id` is a dedupe key, deterministic across Cursor retries
  and replays. `cursor.conversation.id` is the session key.
  `cursor.grok_bot.provenance` is `client` (recorded on the Bot's computer)
  or `server`.
* Exported logs have no `trace_id` / `span_id`.

### How OpenShard maps it

| Cursor event | OpenShard Event | Status |
|---|---|---|
| `grok_bot_shell_command`, `shell.allowed=true` | `tool.invoked` "Shell: ..." | `unknown`: no exit code is exported |
| `grok_bot_shell_command`, `shell.allowed=false` | `approval.denied` (`decided_by = cursor_shell_policy`) | `skipped`: the command did not run |
| `grok_bot_mcp_tool_call` | `tool.invoked` "MCP: server/tool" | `passed` / `failed` from `cursor.tool.status` |
| `grok_bot_browser_navigation` | `tool.invoked` "Browser navigation: host" | `unknown` |
| `grok_bot_computer_use_session` | `tool.invoked` "Computer use session (N actions)" | `unknown` |
| `api_request` | no Event: tokens and model on the record | |
| `api_error` | no Event: counted | |

* One `cursor.conversation.id` = one Shard. Later exports for the same
  conversation extend the same Shard (same `shard_id` and `receipt_id`).
* Re-ingesting the same data is a no-op (dedupe on `cursor.event.id`; the
  last 2,000 ids are kept per conversation).
* At most 500 Events are kept per conversation. Anything beyond that is
  counted, and the record becomes `incomplete` (`dropped_hook_events`).
* Only `cursor.surface=grok_bot` records are read. Other surfaces in the
  same export (IDE, CLI, Cloud Agents) are skipped and counted.
* Never stored: `cursor.user.id`, browser URL paths and queries, page
  titles, shell blocked-reason text, the Grok Bot computer id.
* Metrics (`/v1/metrics`) are accepted and ignored. Their tokens duplicate
  the `api_request` logs, and cost is not attributable to a conversation.

### Deploying it

Cursor needs a public HTTPS endpoint, and OpenShard does not terminate
public TLS. Run an OpenTelemetry Collector as that endpoint, then choose
one of these:

**A. File hand-off (no listener).** The collector writes OTLP/JSON with the
`file` exporter. Ingest it periodically:

```yaml
# otelcol config (excerpt)
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318      # behind your TLS terminator; require the header Cursor sends
exporters:
  file:
    path: /var/lib/otel/cursor-logs.jsonl
service:
  pipelines:
    logs:
      receivers: [otlp]
      exporters: [file]
```

```bash
openshard grok-bot ingest /var/lib/otel/cursor-logs.jsonl --repo /path/to/repo --team-id 12345
```

Ingest is idempotent, so re-reading the whole file is safe.

**B. Receiver.** Forward from the collector with `otlphttp` to OpenShard's
receiver on a private address:

```bash
export OPENSHARD_GROK_BOT_OTLP_TOKEN="$(openssl rand -hex 32)"
openshard grok-bot serve --repo /path/to/repo --host 127.0.0.1 --port 4318 --team-id 12345
```

```yaml
exporters:
  otlphttp/openshard:
    endpoint: http://127.0.0.1:4318
    encoding: proto
    headers:
      Authorization: "Bearer ${env:OPENSHARD_GROK_BOT_OTLP_TOKEN}"
```

The receiver requires the bearer token on every request (constant-time
compare). It refuses browser-originated requests, accepts protobuf or JSON
with optional gzip, and handles one request at a time.

`--repo` chooses which repository's `.openshard/runs.jsonl` receives the
Shards. Grok Bot works in the cloud, so its conversations belong to no
local repository by themselves. `--team-id` rejects records from any other
Cursor team.

## Every plan: the self-report skill

Sources: [Grok Bot for Teams and Enterprise](https://cursor.com/docs/grok-bot/teams),
[Work with Grok Bot](https://cursor.com/docs/grok-bot/work),
[Settings](https://cursor.com/docs/grok-bot/settings), Cursor staff on the
[community forum](https://forum.cursor.com/t/does-grok-bot-support-local-mcp-e-g-workflowy/168182).

What Individual and Teams plans do **not** have: Action Recording,
OpenTelemetry Export and audit logs are all Enterprise-only. Grok Bot has
no hooks. It does not attach MCP servers that run on your machine, whether
stdio or localhost ("The Bot works from a persistent cloud computer, so
those local processes aren't reachable from it"). Its connectors are remote
HTTP/SSE MCP servers and marketplace plugins.

What they do have: **skills** (reusable instructions the Bot follows) and
**Execution on Local Computer**, which lets a Bot run commands on the
desktop running the Grok Bot app, with per-command approval by default.

So the consumer path is a skill that tells the Bot to hand OpenShard a
structured report on your desktop:

```bash
openshard grok-bot skill                  # print SKILL.md; paste it to the Bot ("save this as a skill")
openshard grok-bot skill --output skills/openshard-report/SKILL.md
```

The Bot then runs `openshard grok-bot report -` in your repository and
passes an `openshard.grok_bot.report/v1` JSON document on stdin:

```json
{
  "schema": "openshard.grok_bot.report/v1",
  "report_id": "fix-login-flake",
  "task": "Fix the flaky login test",
  "status": "completed",
  "summary": "Patched the retry logic",
  "actions": [{"kind": "shell", "command": "pytest tests/test_login.py", "result": "passed"}],
  "files_changed": [{"path": "src/login.py", "change_type": "update"}],
  "checks": [{"command": "pytest tests/test_login.py", "result": "passed"}]
}
```

`report_id` (or `conversation_id`) makes the report idempotent: reporting
again updates the same Shard. Commands and text are secret-scrubbed. Every
Event is `agent_reported`. Verification is `source = agent_reported`,
`observation_mode = agent_claim`, and the receipt shows
`1/1 passed (agent claim, not observed)` and `Passed (agent claim)`.

### Why not an OpenShard MCP connector or plugin?

A Grok Bot connector has to be a **remote** MCP server reachable from
Cursor's cloud. OpenShard's MCP server is local and read-only by design. A
write-capable OpenShard endpoint on the public internet (or behind a
tunnel) would give nothing stronger than the self-report. The Bot would
still be the one calling it, so the evidence would still be
`agent_reported`, and it would add a public attack surface. A Cursor
marketplace plugin can only bundle skills, rules and MCP configuration. It
cannot subscribe to the Bot's actions. We therefore ship the skill and the
local CLI, not a connector.

## Trying it locally

No Cursor tenant is needed. `tests/test_grok_bot_capture.py` builds
wire-accurate protobuf exports with `openshard.adapters.otlp_logs.encode_logs_protobuf`
and exercises decode, ingest, dedupe, the receiver and the CLI. To try it
by hand:

```python
from openshard.adapters.otlp_logs import LogRecord, encode_logs_protobuf
rec = LogRecord(
    resource={"service.name": "cursor", "cursor.team.id": 1, "cursor.surface": "grok_bot"},
    body="grok_bot_mcp_tool_call",
    attributes={"cursor.conversation.id": "c1", "cursor.event.id": "e1", "cursor.tool.name": "search",
                "cursor.tool.status": "success", "cursor.grok_bot.mcp.transport": "http",
                "cursor.grok_bot.mcp.duration_ms": 12, "cursor.grok_bot.provenance": "server"},
    time_unix_nano=1_790_000_000_000_000_000,
)
open("export.pb", "wb").write(encode_logs_protobuf([rec]))
```

```bash
openshard grok-bot ingest export.pb && openshard last --more
```

What has **not** been verified: a live export from a real Cursor
Enterprise tenant. The mapping follows Cursor's published wire reference
field by field. Treat the first real export as the acceptance test, and
check `openshard grok-bot ingest --json` for `skipped.unsupported_event`
or `skipped.no_conversation_id`.
