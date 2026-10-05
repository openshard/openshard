# Hosted history for the next cloud task

Fresh checkouts need not have local runs.jsonl history. After upgrading to a release containing this feature, connect an existing organisation read credential using openshard sync connect, then explicitly opt in:

```sh
openshard learn install claude --hosted
# or
openshard learn install codex --hosted
```

Review and enable native hooks in the agent. Capture installation remains separate. Without --hosted, the hook continues using its bounded local snapshot. Reinstalling changes only Openshard's own learning hook; uninstall removes either mode. Do not place organisation read keys in repository files or share them with unrelated agents. Capture-only credentials cannot read history.

On UserPromptSubmit the hook sends the canonical Git origin identity and up to 16 privacy-filtered task terms to the existing linked organisation endpoint. It does not send the raw prompt or transcript. HTTP redirects are refused, responses are capped at 64 KiB, and network waiting including imports/DNS is limited to 900 ms. Missing access, malformed or stale context, timeout and empty history emit no advice and do not stop the task.

The server scans at most 200 recent Receipts for this exact repository and a 90-day window. Advice requires similar task terms and repeated completed, commit-bound observed verification. Unknown, unbound, incomplete and agent-reported checks do not qualify. The current implementation provides descriptive check history; it does not select models, execute recommended checks, change permissions or establish that the advice causes better outcomes.

The native response contains bounded advisory context and supporting Receipt IDs. After successful stdout delivery, a private bounded session sidecar records the handoff. A later ordinary capture in the same current segment can include this evidence. Receipt pages show hosted history and links to supporting Receipts. Model consumption and changed behaviour remain unestablished.

This feature does not repair missing provider telemetry or reconstruct historical cloud usage. Actual Scribe/Tether export coverage, whole-task billing and further runtime integrations remain separate acceptance work.
