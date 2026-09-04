# Scenario 1 — previously failed approach

**Target repository:** `seed/` — *relay*, a small stdlib-only job-queue CLI
(Python 3.11, `python -m unittest`). Built by the benchmark to commit
`d8c94df70eb695cc322f97738adb9a6dc4e710d0` (reproducible; the runner
refuses to proceed if the seed no longer hashes to that id).

**Task (both arms, `evaluation_prompt.txt`):** add an optional integer
`priority` to job records — `relay add ... --priority N`, `relay list`
ordered by priority, old queue files still load, tests added, project
checks pass. The prompt names no files and no mechanism.

**The two ways to do it.** Job record fields live in `schema/jobs.json`,
from which `scripts/gen_schema.py` generates `relay/_schema.py`
(`CONTRIBUTING.md` says so; the generated module carries a header comment;
CI runs `python scripts/gen_schema.py --check`). `Job`, the line parser,
and the `add` command's options are all derived from `FIELDS`, so:

* *correct* — add the field to `schema/jobs.json`, regenerate, adjust
  `QueueFile.ordered()`;
* *known failed approach* (`hand_edited_generated_schema`) — append a
  `Field(...)` to `relay/_schema.py` by hand and adjust ordering. Every
  visible test passes and the feature works, but the generated module is
  now out of sync with its source: the repository's own check fails and
  the source of truth never learns about the field.

Both are natural choices for an agent that has or has not read
`CONTRIBUTING.md` / the header comment.

**Burn-in (`burn_in_prompt.txt`, revision 2 — see below):** the same task,
but with the burn-in agent's changes explicitly closed to
`relay/_schema.py`, `relay/queue.py`, and test files ("no other files in
the repository need to be touched"), which forces the hand-edit of the
generated module deterministically. The prompt never names
`schema/jobs.json`, `scripts/gen_schema.py`, or the generator — it
restricts *where* the agent may write, not by describing the correct
mechanism and telling it not to use that mechanism, since that text would
itself become the burn-in Shard's captured task and would leak the golden
approach to the treatment arm through `relevant_context`. The burn-in
session runs with OpenShard's Claude Code hooks installed and no MCP
server.

**Verification (benchmark-owned, after the agent exits):**

1. `python scripts/gen_schema.py --check` (exit 0 required);
2. `verification/hidden_tests/` via `python -m unittest discover` with the
   workspace as the current directory: `schema/jobs.json` declares
   `priority` as a trailing optional int with default 0; the generated
   module equals the generator's output; records default/parse/format the
   field and omit it when default (old-release compatibility); a v1
   fixture loads with priority 0; the CLI stores, orders and shows it;
   duplicate/remove still work.

**Repeated-known-failure criterion (machine-checked, `mode: all`):**
verification step `generated_schema_in_sync` failed **and**
`relay/_schema.py` is among the changed paths.

**Expected evidence after burn-in:** at least one `claude_code_hooks` Shard
whose task mentions `priority`, whose changed files include
`relay/_schema.py` and exclude `schema/jobs.json`, and which
`relevant_context(<evaluation prompt>)` returns. Hook capture records no
verification outcome, so the Shard shows `Status: Not recorded`; the
evidence available to the treatment arm is the prior attempt's existence,
its task text and the files it touched.

**What is deliberately not in the repository:** the hidden tests and the
criterion. What *is* in the repository and discoverable by either arm:
`CONTRIBUTING.md`, the generated-file header, the generator script and
its `--check` mode, and the compatibility rules.

## Revision history

**Revision 2 (2026-09-04).** Pilot 0 ran this scenario's original
burn-in prompt through the real Claude Code CLI with Claude Sonnet 5 as
the burn-in model. Sonnet independently read `CONTRIBUTING.md`, found the
generator workflow, and implemented the feature correctly instead of
taking the known-bad shortcut — `require_known_failed_approach` was not
satisfied, the benchmark aborted (`burn_in_did_not_fail`), and **zero**
control/treatment arms ran; no A/B outcome of any kind was observed. The
burn-in prompt was revised at that point — before any arm result existed
to react to — to explicitly close the burn-in agent's file scope to
`relay/_schema.py`, `relay/queue.py`, and test files, which forces the
known failed approach without naming `schema/jobs.json` or the generator
(a change that would leak the correct approach to the treatment arm
through the captured Shard's task text). See `metadata.json`'s
`revision` field for the machine-readable record. Nothing about the
evaluation prompt, verification steps, the `known_failed_approach`
criteria, or `expected_evidence` changed.
