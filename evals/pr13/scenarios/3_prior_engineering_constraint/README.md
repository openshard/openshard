# Scenario 3 — Prior engineering constraint

**Target repository:** the shared relay seed (`../_shared/relay_seed/`,
commit `d8c94df7…`).

**What this scenario tests.** Whether treatment can carry forward a
*non-obvious engineering constraint* discovered on a past, different task
to a new task in the same area, rather than the exact-repeat trap
Scenario 1 uses. The constraint is real and platform-observed, not
invented: on this benchmark's own machine, `open(path, "w")` /
`Path.write_text(...)` without `newline=""` or `newline="\n"` silently
translates `\n` to `\r\n` on write. relay's own queue-file writer
(`QueueFile.save`) already avoids this (it passes `newline="\n"`); a new
file-writing command that skips that discipline produces CRLF output that
`git diff`/plain-text tooling won't visibly flag but a byte-level check
will.

**Burn-in (`burn_in_prompt.txt`):** add `relay export PATH` (CSV of every
job), explicitly instructed to write with a plain `Path(path).write_text
(csv_text, encoding="utf-8")` — no `newline=` control. This deterministically
produces CRLF output (verified directly on this machine: a plain
`write_text` call reproduces `\r\n`; `newline=""` avoids it). Verification
fails on a byte-level CRLF check.

**Evaluation (`evaluation_prompt.txt`, both arms):** add `relay snapshot
PATH` — a full dump of the current queue to PATH, in the queue file's own
tab-separated format. Names no files, says nothing about encodings, line
endings, or platforms.

**Verification (shared across stages, skip-based):** one hidden-test
suite covers both `export` (burn-in) and `snapshot` (evaluation); each
`TestCase` skips itself when its own command isn't registered (`export`
does not exist post-reset). Checks: no `\r\n` bytes in the written file,
correct content, and (for `snapshot`) that the output matches the queue
file's own on-disk format and the queue file itself is untouched.
Sanity-checked directly against hand-written buggy (`write_text` with no
`newline=`) and correct (`format_line` + `newline="\n"`) implementations
of both commands before finalizing.

**Repeated-known-failure criterion:** verification step `hidden_tests`
failed **and** `relay/cli.py` is among the changed paths.

**Expected effect.** Plausible, modest help for treatment: it can see
that a past attempt at a related file-writing feature ran into a
line-ending bug, without being told the fix in the eval task's own
prompt. Unlike Scenario 1, control is not deliberately set up to fail —
`snapshot`'s correct implementation is independently reachable by reading
`queue.py`'s existing `format_line`/`save` functions, so this scenario
tests *transfer*, not a forced trap.
