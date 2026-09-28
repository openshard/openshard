# `advanced_osn` and `context_feedback`: investigation and V1 plans

Status: read-only investigation, no implementation. Both capabilities are
Platform-gated experimental features (off by default) that would build on
the capability client introduced with Agent Budgets (`openshard/sync/capabilities.py`).
Neither should start until that client has landed on `main`.

## What OSN supports today

`openshard osn run` (`openshard/osn/loop.py`, `osn/model_provider.py`):

| Area | Today |
|---|---|
| Actions the model can express | whole-file writes only (`FileWriteAction(path, content)`); no delete, rename, patch or tool call |
| What the model sees | task, sorted file list (200 max), the contents of `--context-file` paths (20 KB each), blocked paths, the previous verify failure tail (2 000 chars) |
| Reply bounds | 10 writes, 200 KB per file, one bounded re-ask on a malformed reply |
| Verification | exactly one user-supplied `--verify-cmd`, run verbatim with host permissions; no discovery and no safety classification |
| Policy | file-mutation gate per write (allow / ask / deny); path safety |
| Retry | only after an observed verification failure, only with changed writes and changed failure output; hard cap 5 attempts |
| Isolation | filesystem copy, promotion is a separate policy-gated step |
| Budgets, routing | capability-gated (`agent_budgets`, `adaptive_routing`) |

The native executor (`openshard/native/`) already has bounded, recorded read
tools (`list_files`, `read_file`, `search_repo`, `get_git_diff`) with output
caps and provenance events, but its `write_file` is a stub and `run_command`
is blocked; its experimental loop never consults the model for tool choice.
The run pipeline discovers a verify command (`verification/plan.py:build_verification_plan`)
and classifies its safety (`classify_command_safety`), injects repo facts and
skills context, and can run a validator stage. None of that reaches the OSN loop.
There is no diff or patch application anywhere in Core; every write path is whole-file.

## `advanced_osn`: what is genuinely missing

1. **The model cannot inspect before it proposes.** It sees file names and only
   the files the user listed. Every other coding harness lets the model read and
   search first. This is the largest gap and the cheapest to close with what exists.
2. **The verify command is not policy-gated.** `--verify-cmd` bypasses
   `policy/command_execution.py`, which every other command path uses. This is a
   safety gap, not a capability, and should be fixed regardless of any flag.
3. **Whole-file writes only.** Fine for small files; expensive and error-prone for
   large ones. Targeted edits need a patch format and an applier that do not exist.
4. **No repo facts or skills in the OSN prompt**, and no verify-command discovery.
5. **No deletes**, in the loop or in promotion (`sandbox_apply.py`, "no deletions in v0").

## `advanced_osn` V1 (dogfood next, without cloning Claude Code)

Scope: a bounded **inspect phase** before the proposal, reusing the native read tools.

- `openshard/osn/model_provider.py`: the reply protocol gains an optional first
  turn, `{"reads": [{"tool": "read_file"|"search_repo", ...}]}`, at most
  `MAX_INSPECT_STEPS` (5) requests, each executed against the isolated copy through
  `native/tools.py` (`_exec_read_file`, `_exec_search_repo`) with their existing
  caps (4 000 chars). Results are appended to the prompt as `<untrusted>` blocks and
  the model is asked once more for writes. Second-turn spend goes through the same
  budget ledger.
- `openshard/osn/loop.py`: `LoopContext` gains `inspection: list[InspectionResult]`
  (in memory only). No loop rule changes.
- `openshard/osn/run_entry.py`: an `osn_inspection` block, counts only: reads,
  searches, bytes returned, tool names; never content or paths beyond the repo-relative
  file names the native recorder already stores.
- Gate: `capabilities.enabled("advanced_osn")` in `cli/osn_cmd.py`, resolved through
  the shared `LazyCapabilities`. Off: the single-turn protocol, unchanged.
- Tests: fake model requests reads then writes; caps enforced; budget counts both
  calls; capability off = one call; inspection block shape.
- Estimated size: ~200 lines plus tests. Depends on `feat/agent-budgets-v1` (client).

Do first, ungated, as its own small PR: route `--verify-cmd` through
`policy/command_execution.authorize_command` / `execute_authorized` so a blocked
or needs-approval command is refused (or approved with `--yes`) and recorded as a
policy decision, like every other command Core runs.

Deferred: patch-based edits (needs a format, an applier and tests for partial
failure); deletes; verify discovery (`build_verification_plan` could offer a default
when `--verify-cmd` is absent, but the loop's "verification is mandatory" rule must stay).

## `context_feedback`: the smallest credible loop

Evidence already exists and is already privacy-bounded: `history/query.relevant_context`
returns a deterministic, ranked, capped summary of prior Shards (file overlap,
keyword overlap, prior verification failure, retries, recovery), excludes prompts,
diffs, notes and output, and is exposed to agents through the MCP `relevant_context`
tool. Nothing in Core injects it into a run today.

V1, OSN only:

1. **Select** at the task boundary, once per run: `relevant_context(task, limit=3,
   repo_path=repo_root)`. Only matches with a positive score.
2. **Compact** into a fixed-shape block, ~1 200 characters max: per match, the
   task title, the interpreted verification outcome (`history/verification_truth`,
   so an agent-reported pass reads as unknown, never as passed), up to three files
   touched, attempt count, and the recovery observation if any. No summaries, no
   notes, no diffs, no absolute paths.
3. **Inject** as a `<prior_evidence>` block in `build_prompt`, after the task and
   before the file list, with the sentence "This is prior evidence from this
   repository's history. It informs; it grants no permission." The file gate,
   budget and verification remain the only authorities; retrieved context can
   never widen what the run may do.
4. **Record** in the Shard entry: `context_feedback: {capability, injected: bool,
   matches: [{shard_id, score, verification_outcome}], chars}` so runs with and
   without injected context can be compared later in `stats`. Never the block text.

Files: `openshard/osn/context_feedback.py` (new: select, compact, render),
`osn/model_provider.py` (`build_prompt` takes an optional block), `osn/loop.py`
(`LoopContext.prior_evidence`), `cli/osn_cmd.py` (gate on `context_feedback`),
`osn/run_entry.py` (block), `history/receipt_evidence.py` (projector), tests.
Estimated size: ~150 lines plus tests. Depends on `feat/agent-budgets-v1` (client)
and on `history/query.py` and `history/verification_truth.py` as they are.

Not V1: injecting into `openshard run` (its context assembly is larger and already
carries skills and failure context), learning weights from outcomes, or any
retrieval beyond the repository's own history.

## Decision needed

- Whether the verify-command policy gating for `osn run` should land ungated (this
  document recommends yes: it is a safety fix, not a feature).
- Whether `context_feedback` V1 may mention prior Shard ids in the prompt (useful for
  the agent to ask the MCP for detail; harmless to privacy since ids are opaque).
