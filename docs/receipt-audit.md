# Receipt completeness audit

`scripts/receipt_audit.py` audits an organisation's hosted Receipts and runs a recovery dry run. It changes nothing: the Platform client can only send `GET`, and every recovery candidate names its original evidence without applying it.

```bash
python scripts/receipt_audit.py snapshot --out inv/receipts.json            # OPENSHARD_API_KEY/OPENSHARD_ORG_ID, or the linked ~/.openshard/platform.json
python scripts/receipt_audit.py evidence --repo . --repo ../platform --out inv/github_evidence.json
python scripts/receipt_audit.py sessions --receipts inv/receipts.json --history .openshard/runs.jsonl --out inv/sessions.json
python scripts/receipt_audit.py report --receipts inv/receipts.json --evidence inv/github_evidence.json \
  --sessions inv/sessions.json --out-json inv/report.json --out-md inv/report.md
```

Org API keys act as `member`, which is the lowest role that can read Receipts. No read-only key scope exists. Keep the snapshot private: it holds the organisation's Receipt data.

## Rules

- Values are counted only when recorded, on the Receipt or in its later usage or verification evidence. Missing is never zero.
- `GitHub observed cloud work` and `unknown` count as missing agent attribution.
- Per-commit values come only from per-commit evidence: `Openshard-*` trailers, or the model name in Claude Code's co-author trailer on a Claude Code Receipt. Session totals are never divided across commits.
- Session usage comes from that session's own local transcript, and only when all of these hold:
  - the session has one Receipt segment;
  - its end was observed;
  - the transcript's usage lies inside the Receipt's window.

  Otherwise the candidate is marked for review.
- A `Claude-Session` trailer is only a claim. Agent attribution cannot be added to an existing Receipt.

Receipts are immutable. Recovered tokens or models can only be added as later usage evidence (`imported_transcript` or `agent_reported` attestations), and only after the specific plan is approved.
