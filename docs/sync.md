# Receipt sync (opt-in)

OpenShard is local-first. Receipts live in `.openshard/runs.jsonl` and never
leave the machine unless you turn sync on. Sync pushes receipts to an
OpenShard Cloud endpoint so a team can see them in the hosted dashboard.

## Turning it on

```bash
export OPENSHARD_SYNC_ENDPOINT=https://cloud.example.com   # or sync: {endpoint: ...} in .openshard/config.yml
export OPENSHARD_SYNC_TOKEN=<api token from the dashboard>  # env var only, never a config file
openshard sync status
openshard sync push --dry-run     # see what would be sent
openshard sync push               # send new or changed receipts
```

Nothing is sent unless both the endpoint and the token are set. The token
is read from the environment only; it is never written to a config file or
printed. Plain `http://` is refused except to loopback for local
development.

## What is sent

One envelope per run record (`openshard/contracts/sync.py`):

| Key | Content |
| --- | --- |
| `receipt_contract` | Receipt Contract v2 (`docs/architecture/RECEIPT_CONTRACT_V2.md`) |
| `receipt` | the same bounded projection the MCP server exposes (`receipt_to_dict(extended=True)`) |
| `shard_id`, `run_id`, `attempt_number`, `repo_identity`, `content_hash` | identity and integrity |
| `openshard_version`, `sent_at`, `envelope_version` | provenance |

Never sent: raw prompts, transcripts, diffs, file contents, stdout/stderr,
absolute paths, environment values, secrets. The projections are built by
the same code that already enforces this for `openshard history --json`.

## Idempotence

`.openshard/sync_state.json` records, per run, the `content_hash` and the
remote id the server returned. `openshard sync push` skips records whose
hash has not changed; `--force` re-sends them. A record that changed
locally (a new hash) is sent again and the server updates it. Outcomes
recorded later with `openshard outcome record` are attached to the next
push of that Shard.

## Failure handling

Each push is one POST with strict timeouts. Failures are reported by
category (`auth`, `transport`, `http_<code>`), never with raw error text,
and the run stays pending for the next push. Sync never runs from an agent
hook or the capture service.
