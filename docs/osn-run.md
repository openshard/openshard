# `openshard osn run`

A bounded, policy-gated coding task with verification performed by OpenShard.

```
openshard osn run "Implement slugify in slug.py" \
  --verify-cmd "python -m pytest -q tests" \
  --context-file slug.py --context-file tests/test_slug.py \
  [--model M] [--escalate-model M2 ...] [--max-attempts 2] \
  [--task-id task_...] [--promote] [--yes] [--json]
```

## Flow

task -> context -> model proposes whole-file writes -> policy gate -> isolated copy ->
OpenShard runs `--verify-cmd` -> bounded retry / escalation -> receipt.

- The model is called through the existing provider layer (`BaseProvider.execute`), so any
  configured provider works. Without `--model`, the existing keyword routing picks the first model.
- Writes go only to an isolated copy of the repository (local secrets and agent state such as
  `.env` and `.claude/` are not copied). Your repository is untouched unless `--promote` is given
  and the loop's own verification passed.
- Every proposed path passes path-safety checks and the file-mutation policy (deny: secrets,
  `.env*`, `.git/`, `.openshard/`; ask: CI, Docker, `pyproject.toml`, `package.json`). A blocked
  proposal is not retried.
- OpenShard runs the verify command itself and reads its exit code. A leading `python` runs under
  the interpreter OpenShard uses. The verifier runs with your permissions and can execute
  agent-written code: the copy isolates files, not processes.
- A retry happens only after a verification failure, only if the proposed writes and the failure
  output both changed, and never more than 5 attempts. `--escalate-model` models are used only for
  those retries.
- `--promote` copies the verified files into the repository through the same policy gate as
  `apply-last`. It refuses if the files changed after verification, and it does not re-verify in
  the repository.

## Evidence

| Field | Level |
|---|---|
| Proposed writes | agent-declared |
| Policy decisions, file effects | OpenShard-observed |
| Verification (exit code) | OpenShard-observed (`directly_observed` / `openshard_executed`) |
| Model cost | recorded only when the provider reported it; otherwise unknown |

A verifier that cannot be started is recorded as `not_run`, never as a pass or a model failure. A
verifier that rewrites the files it is checking does not count as a pass. The Shard entry
(`executor: osn_loop`) stores no task text beyond the usual sanitised task, only the verifier's
executable name, and a shadow routing provenance block, so `openshard stats routing` includes
these runs. The model that ran is chosen by you or by keyword routing, not by adaptive routing.
