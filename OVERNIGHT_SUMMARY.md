# Overnight summary: v0.5.0 foundation

Branch `claude/openshard-v050-foundation-bn9gfk` (pushed). Nothing was pushed
to `main`. The private cloud code is **not** in the public repo.

## 1. What I built

- **Receipt Contract v2** (`openshard/history/receipt_contract.py`): a
  read-time projection that answers the nineteen questions (owner,
  requester, executor, permissions, policy, approval and approver,
  verification and independence, capture completeness, generation /
  verification / retry cost, cost per verified success, outcome, integrity)
  and derives one of eight receipt states with a stated reason.
- **Optional additive record blocks** (`actors`, `permissions`, `policy`,
  `approval`, `verifiers`, `escalation`, `cost_breakdown`, `outcome`,
  `attestation`). v0.4.3 records are untouched and still build a full
  contract; `schema_version` stays `"1.2"`.
- **Surfaces**: `RECEIPT STATE` in the full receipt, `RECEIPT ANSWERS` in
  `openshard last --full`, `receipt_contract` in `last --json`, `state` in
  `history --json`; the compact receipt is byte-identical for old records.
- **Contracts package** (`openshard/contracts/`): Protocols for verification,
  policy (ALLOW/DENY/ASK with policy identity and reason), approvals
  (approver as its own principal, mechanism, timestamp), sync, Managed
  Compute and outcomes; each result maps onto a receipt block. Trivial
  implementations only.
- **Outcomes**: `openshard outcome record <shard> merged --reference "PR #341"`
  appends beside the run record, so the content hash stays valid.
- **Sync client**: `openshard sync status|push` (opt-in, HTTPS only, token
  from the environment only, idempotent, never on a hook path).
- **OpenShard Cloud** (private, separate repo): FastAPI + SQLAlchemy 2 +
  Alembic + Jinja2; 17 tables covering User, Organisation, Membership, Team,
  Project, Agent, ApiToken, Receipt, ReceiptAttempt, Evidence, Verification,
  Policy, PolicyDecision, Approval, ManagedComputeRun, UsageRecord; token
  auth; idempotent ingest that rejects blocked fields; receipt API;
  dashboard with list, state filters, cost tiles and a detail page; operator
  CLI; demo seed labelled as demo.
- **Docs**: foundation audit, receipt contract, contracts, future features
  (A–H), sync; cloud README (public/private boundary) and roadmap.

## 2. What works end to end

Verified by automated tests in both repos and by a manual smoke run:

```
fixtures as .openshard/runs.jsonl
  -> openshard outcome record ... merged
  -> openshard sync push            (4 receipts sent, second push: 4 unchanged)
  -> POST /api/v1/sync/receipts     (bearer token, 201 created / unchanged)
  -> SQLite via SQLAlchemy          (receipt + attempts + verifications + usage)
  -> GET /api/v1/receipts           (states, outcome merged, attempts=2 for the escalation)
  -> /login -> /receipts -> /receipts/{id}   (VERIFIED (after escalation), BLOCKED, PR #341 visible)
```

`openshard-cloud init-db && seed-demo && serve` then login works from a
fresh directory.

## 3. What remains incomplete

- Producers (pipeline, hook adapters) do not yet write the new blocks; the
  contract derives from existing fields and the fixtures show the target
  shape. This is the first follow-up.
- No hosted approvals, policy evaluation, RBAC beyond membership, Managed
  Compute provider, or outcome ingestion from GitHub/CI. Interfaces and
  TODOs only (`docs/architecture/V050_FUTURE_FEATURES.md`,
  `openshard-cloud/docs/ROADMAP.md`).
- Dashboard: no pagination/search, no CSRF token on the login form (cookie
  is `SameSite=Lax`, `HttpOnly`, `Secure` outside development), no login
  rate limiting, no password reset.
- Attestation (signing) is a reserved slot only.

## 4. What changed in the public repo

Commits on the branch (oldest first): foundation audit; Receipt Contract v2;
contracts + outcomes + sync client; sync-state key fix; docs and progress;
final review fixes. 31+ files, ~5,000 lines added, ~10 removed. Modified
existing files: `openshard/history/shard_contract.py` (three new optional
fields on `ShardReceipt`, one conditional row, one full-view block),
`openshard/history/views.py` (two extended keys), `openshard/cli/main.py`
(additive `--json` key, `outcome` and `sync` groups, form-factor hardening),
`CHANGELOG.md`, `README.md` (one line), `docs/cli-reference.md`.

## 5. What exists in openshard-cloud

Local git repository at `/home/user/openshard-cloud` with three commits,
delivered as `openshard-cloud.bundle` (full history) and
`openshard-cloud-src.tar.gz`. **Creating the private GitHub repository was
blocked**: the session's GitHub integration returned 403 for both the
`openshard` organisation and the personal account. To publish:

```bash
gh repo create openshard/openshard-cloud --private   # or via the GitHub UI; keep it private
git clone openshard-cloud.bundle openshard-cloud && cd openshard-cloud
git remote set-url origin git@github.com:openshard/openshard-cloud.git && git push -u origin main
```

No secrets are in it; `.env.example` lists names only.

## 6. Test results

- Public repo: 8834 passed / 3 skipped before any change; see the final line
  below after all changes. Ruff and mypy clean.
- openshard-cloud: 19 passed (ingest, API, dashboard, operator CLI, live
  loopback end-to-end with the public sync client). Ruff clean.

Final public run after all changes: **8909 passed, 3 skipped** (883 subtests passed), ruff and mypy clean.

## 7. Important architecture decisions

1. State is derived, never persisted, in a fixed priority order; approval
   never counts as verification; a policy deny beats a granted approval.
2. Everything new on the record is optional and additive; the contract has
   its own version (`2.0`).
3. Outcomes live beside the record (`outcomes.jsonl`) to keep hashes valid.
4. Sync sends only the two existing privacy projections; the server stores
   the client's contract verbatim and never re-derives state.
5. Owner, requester, executor and approver are separate principals; none is
   inferred from another.
6. Python stack for the cloud so the receipt code path is shared and the
   first slice needs no JS build.

## 8. Anything risky

- `ShardReceipt` gained three fields; any external code constructing it
  positionally would break (none in the repo; all callers use keywords).
- The compact receipt shows a `State` row once a producer writes any v2
  block; that is a visible change for future records, by design.
- Sync is a new egress path. It is off by default, HTTPS-only, token via
  env only, and tested to refuse plain HTTP and to omit blocked fields, but
  it should get a second pair of eyes before any release.
- Cloud login lacks CSRF and rate limiting; do not expose a deployment
  publicly before adding both.
- The development secret key is a fixed constant; production refuses it.

## 9. Next five highest-value tasks

1. Make producers write the v2 blocks: `actors` from config/env, `policy`
   from a `GatePolicy` wrapper around today's gates, `approval` from a
   `CliPromptApprovalProvider`, `verifiers` from the pipeline / OSN runner,
   `permissions.used` from the tool trace.
2. Hosted approvals: `POST /api/v1/approvals`, dashboard inbox, `openshard
   approval wait`, receipt stamped with approver and mechanism `dashboard`.
3. Policy definition format evaluated identically offline and hosted, with
   `GET /api/v1/policies/effective`.
4. Coverage report (`openshard stats coverage` + hosted per-project view)
   comparing receipts with independent repo signals, so the product never
   implies 100 % observation.
5. Push `openshard-cloud` to a private repo, add CI (ruff + pytest), CSRF
   and login rate limiting, then a PostgreSQL run of the migration.
