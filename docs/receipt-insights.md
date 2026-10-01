# Receipt insights

`openshard insights` turns the Receipt history already stored in
`.openshard/runs.jsonl` into fast local analytics and relationships.

The important boundary is unchanged:

- `runs.jsonl` is still the canonical local evidence record.
- DuckDB is an in-memory analytical projection, rebuilt on demand.
- Nothing in `openshard insights` writes back to a Receipt.
- Unknown cost is excluded, never treated as `$0`.
- Pass/fail rates only use independently observed verification.
- One Receipt is an anecdote. Plain-English questions do not name a model or
  agent leader until there are at least two observed runs for that subject.
- Raw prompts, transcripts, command output and environment values are not
  copied into the analytical tables or graph.

## Commands

```bash
openshard insights overview
openshard insights models
openshard insights models --task visual
openshard insights agents
openshard insights costs --by model
openshard insights checks
openshard insights failures
openshard insights learning
openshard insights graph
openshard insights graph --find rcpt_...
openshard insights ask "Which model performs best?"
openshard insights ask "What is costing us the most?"
```

Every command supports a machine-readable JSON form where useful.

## Receipt graph

The graph is rebuilt from the same analytics snapshot. It links a Receipt to
the evidence it actually recorded:

```text
Receipt -> Agent
Receipt -> Model
Receipt -> Task category
Receipt -> Repository
Receipt -> Changed file
Receipt -> Verification check
Receipt -> Policy decision
```

A direct Receipt-to-Receipt edge is only added when both Receipts are recorded
as changing the same repo-relative file. It is a relationship, not a claim of
causality or dependency.

This graph is deliberately not a second graph database. It is a derived view
that can later power local MCP/OSN context and the hosted Platform experience
without changing what a Receipt means.
