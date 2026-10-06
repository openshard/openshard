# Runtime capture audit — 4 October 2026

This table describes the current Openshard adapters, not a promise about every
provider's current or future platform. Capture requires installing hooks in
the actual agent environment and authorising delivery before work starts.
A commit's author/model label cannot replace runtime usage evidence.

| Runtime adapter | Model evidence | Token evidence | Cost evidence |
|---|---|---|---|
| Claude Code | Hook, status line or readable session transcript | Provider usage in readable session transcript | Reported status-line estimate, or Openshard estimate from complete transcript usage and known pricing |
| Codex hooks / App Server | Active model slug in hooks; provider from a matching runtime transcript when present | Cumulative runtime `token_count` from the hook-provided transcript, or App Server `thread/tokenUsage/updated` after exact thread/session-id validation | Dated list-rate estimate only when cumulative usage belongs to one known priced model; otherwise unknown |
| OpenCode | Assistant message reports | Per-message reports | Per-message reports; unpriced zero is unknown |
| Cursor hooks | Model reported by hook | Not exposed by this adapter's hooks | Not exposed by this adapter's hooks |
| Antigravity hooks | Model reported by hook | Not exposed by this adapter's hooks | Not exposed by this adapter's hooks |
| Grok Build hooks | Not exposed by this adapter's hooks | Not exposed by this adapter's hooks | Not exposed by this adapter's hooks |
| Hermes hooks | Provider/model in requests | Per-request usage reports | Not exposed by this adapter's hooks |

Readable Claude and Codex historical logs also have ingestion parsers. Their
availability on a local machine does not imply availability in ChatGPT Work,
mobile Codex or another cloud session. Destroyed sessions cannot be recreated
from commits alone.

## OpenAI surfaces and economics

ChatGPT Chat, ChatGPT Work and Codex are not collapsed into one execution
surface. They can be rolled up into the OpenAI product family for Insights,
while the Receipt keeps the exact agent/surface and model that evidence named.

Work and Codex share OpenAI's usage structure. Codex runtime evidence can be
captured from hooks/transcripts and, when available, reconciled later from the
App Server's cumulative thread token notification. The App Server path binds
only when its thread id exactly equals a Codex session id already observed by
Openshard; timing, PR, branch and task similarity never create a match.

Regular Chat is different. A ChatGPT conversation that happens to edit GitHub
through a connector is not promoted to a Codex thread. GitHub/CI evidence can
prove the resulting commit, but without provider/runtime token evidence its
token usage and execution cost remain unknown. This is a provider visibility
boundary, not a reason to invent a number.

For ROI, three concepts must remain separate:

* **reported/reconciled cost** — dollars or credits the execution/provider
  surface actually reported for the bound run;
* **estimated compute cost** — token usage multiplied by a dated public list
  rate, always labelled estimated and never presented as a ChatGPT invoice;
* **unknown cost** — no defensible per-run economic evidence exists.

A future subscription/seat allocation may add an accounting view, but it must
not overwrite execution cost: allocating a fixed monthly plan fee across tasks
is useful economics, not provider-reported model spend.

## Confirmed defect and fix

Persistent connected sessions previously shared the same local upload spool.
Starting another session could replace queued evidence before it was delivered.
Each connected session now has its own durable journal. Flush drains all those
journals, delivery selects only each session's own Receipts, and an organisation
change blocks delivery of the earlier organisation's queue. Existing root
journals remain readable. This fixes a demonstrated queue defect; it does not
establish that this defect caused any particular missing hosted run.

## Scribe and Tether inspection

Scribe's main branch contains recent Claude-authored commits and an OSN config,
but no committed hook setup. There is no GitHub Actions run history to establish
capture startup or delivery. Tether's inspected main tree also has no committed
capture setup. Hook settings may have existed only in the cloud session, so
absence from git is not proof they were absent at runtime. The original session's
hook/delivery diagnostics are still needed to attribute the missing runs.

## What is still required

* For runtimes with unreadable usage: a documented session export, usage API or
  provider hook that reports usage for the same session/task. An estimate must
  identify its rate source and coverage; account-wide billing is not task cost.
* For Herdr, Replicas, JCode or another future runtime: an actual sample/export
  and access contract before choosing an adapter. Existing compatible Receipt
  envelopes can use the sync contract, but names alone do not imply support.
* For learning: OSN already supplies bounded relevant history to later runs and
  can change routing when its evidence gate is met. Hosted Receipts now carry
  its bounded evidence. External agents still need an explicit context-delivery
  integration; publishing recommendations alone does not apply them.
* For proof of improvement: compare subsequent outcomes with appropriate task
  and environment controls. Context delivery and correlation do not prove ROI.
