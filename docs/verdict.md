# `openshard verdict`

One machine-readable answer to "is this verified?" for a commit or a task,
from the evidence already recorded in this repository. Nothing runs and
nothing is written; `openshard verify` is what produces evidence.

```sh
openshard verdict --commit <sha> --json
openshard verdict --task <task_id> --json
```

## Exit codes

| Code | Verdict | Meaning |
|---|---|---|
| 0 | `verified` | an outcome OpenShard (a re-run) or CI observed passed |
| 1 | `failed` | the current outcome is a failure (`authority` says who reported it) |
| 2 | `unknown` | not checked yet, partial, not observed, or only the agent says it passed |
| 3 | `error` | the question could not be answered (bad commit or task id, bad options) |

Usage errors exit 3, never 2, so 2 always means "unknown".

## What counts

- `--commit`: only `openshard verify` re-runs and CI verdicts bound to exactly
  that commit (`.openshard/verifications.jsonl`). A re-run on a dirty tree is
  not bound to a commit and does not count. The newest conclusive evidence
  wins, by the same rules `openshard last` uses (`history/verification_truth.py`).
- `--task`: the task's latest Receipt, with the later evidence recorded for
  it. Earlier Receipts are listed in `receipts`, never used for the verdict.
- A Receipt whose content hash no longer matches is `unknown`
  (`reason: integrity_mismatch`).

## Document (schema version 1)

```json
{
  "schema": "openshard.verdict",
  "schema_version": 1,
  "subject": {"kind": "commit", "id": "<full sha>"},
  "verdict": "verified",
  "exit_code": 0,
  "reason": "verified_passed",
  "state": "verified_passed",
  "authority": "directly_observed",
  "basis": "post_session",
  "artifact_sha": "<sha the evidence is bound to, or null>",
  "evidence": [
    {"attestation_id": "vat_...", "receipt_id": "rcpt_...", "kind": "rerun",
     "status": "passed", "source": "directly_observed",
     "artifact_sha": "<sha>", "created_at": "2026-10-18T11:00:01Z"}
  ],
  "receipts": []
}
```

- `reason` is the verification state (`history/verification_truth.py`
  `STATES`), or `not_checked`, `no_receipt`, `integrity_mismatch`, `error`.
- `authority` is `directly_observed`, `independently_verified`,
  `git_verified`, `agent_reported` or `none`.
- `--task` adds `receipt_id` (the latest Receipt) and fills `receipts` with
  `{receipt_id, verdict, state, authority}` per Receipt, oldest first.
- `error` adds `message`.

Fields are only ever added within a schema version; a change to the meaning
of a field bumps `schema_version`.
