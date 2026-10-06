# Economic cost evidence

OpenShard keeps runtime Receipts concrete but lets Insights aggregate related
surfaces. In particular, ChatGPT Work, Codex CLI and Codex Cloud can roll up to
the OpenAI provider family without erasing which surface produced a Receipt.

Cost is an evidence hierarchy, not one ambiguous number:

1. `provider_billed` — provider-reported credits/dollars attributable to the task.
2. `token_equivalent` — observed tokens priced against a dated official rate.
3. `subscription_allocated` — an explicitly allocated share of a subscription.

The strongest available figure may be used for economic analysis, but its kind
and source must remain visible. Allocated subscription cost is never labelled as
provider spend or an invoice. Unknown provider cost is never treated as zero.

For OpenAI Work/Codex, prefer session-matched runtime token evidence and
provider-reported chat credits when a supported interface supplies them. Regular
ChatGPT Chat currently remains a separate surface: do not infer its task-level
tokens from Codex/Work allowances.

This model is additive to Receipt integrity. Raw agent/surface identity remains
available for questions such as "does Codex outperform Work?", while provider
rollups answer "what is our OpenAI-family ROI?"
