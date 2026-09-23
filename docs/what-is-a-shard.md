# What is a Receipt?

**A Receipt is the saved record of an AI coding run.**

When Claude Code, Codex, Cursor, OpenCode, Google Antigravity, Hermes Agent,
Grok Build or Grok Bot writes or changes code, Openshard records what
happened: the task, the agent and model where known, the files touched, the
checks that ran, the result, the estimated cost, and how completely
Openshard could see the run.

The `receipt_id` is what makes a Receipt globally unique and portable across
repositories, machines and organisations; the older `shard_id` remains on
every record for compatibility with existing repo history.

## What a Receipt proves

A Receipt does not prove the code is correct. Nothing about a finished agent
turn does.

What it proves is more practical:

- what the agent was asked to do
- what Openshard recorded during the run
- what changed, and whether that was agent-reported or only git-observed
- which checks passed, failed, were skipped, or never ran
- whether the saved record was changed later

That matters because AI coding should not be a black box.

## Checks

Checks are the tests and validations run during the work — things like
formatting, linting, or build steps. The Receipt records, for each one,
whether it **passed**, **failed**, was **skipped**, or was **not run**. It
does not hide a failed check.

## The hash is a fingerprint

Every Receipt can carry a content fingerprint. If the record is changed
later, the fingerprint no longer matches, so you can tell something moved.

That's all it is. The hash is **not a signature**, and it is **not
blockchain proof**. It's a simple way to check that the receipt you're
reading is the one that was saved.

## Openshard doesn't guess

If the model is unknown, the Receipt says `unknown`. If cost was not
captured, it says `not recorded`. If part of a session was missed, the
Receipt records a partial capture rather than filling the gap.

## Where Openshard fits

The aim of Openshard is not to compete with Claude Code, Codex, Cursor or
OpenCode at writing code. Those tools do the coding. Openshard sits around
the run and keeps the record of what happened, first locally, and — once
you connect a hosted Openshard organisation — synced into a shared history
your team can see.

Agents write code. Openshard keeps the receipt.
