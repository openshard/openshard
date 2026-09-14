# Overnight progress (v0.5 foundation)

Branch: `claude/openshard-v050-foundation-bn9gfk` (public repo).
Private repo: `openshard-cloud` (local git repo in this session; see Blocked).

## Completed

- Stage 0: `docs/architecture/V050_FOUNDATION_AUDIT.md` (what exists, reuse,
  gaps, do-not-rebuild, compatibility risks, recommended architecture,
  adapter capability matrix).
- Stage 1: Receipt Contract v2 (`openshard/history/receipt_contract.py`),
  eight receipt states derived from evidence with reasons, optional additive
  record blocks, `ShardReceipt.receipt_state`, renderer and `--json`
  surfaces, five scenario fixtures, `docs/architecture/RECEIPT_CONTRACT_V2.md`,
  `tests/test_receipt_contract.py`.
- Stage 2: `openshard/contracts/` (verification, policy, approvals, sync,
  compute, outcomes), `docs/architecture/V050_CONTRACTS.md`,
  `tests/test_contracts.py`; local outcome recorder (`openshard outcome
  record`, `.openshard/outcomes.jsonl`).
- Stage 4/5 public half: `openshard/sync/` + `openshard sync status|push`,
  `docs/sync.md`, `tests/test_sync_client.py` (includes a loopback HTTP
  server test and the CLI end to end).
- Stage 4/5 private half: `openshard-cloud` package (FastAPI, SQLAlchemy 2,
  Alembic, Jinja2): 17 tables, token auth, idempotent ingest, receipt
  list/detail API, dashboard (list, filters, cost tiles, detail with state,
  actors, policy, approval, verification evidence, cost, attempts, outcome,
  integrity), operator CLI, demo seed, tests including a live end-to-end run
  of the public sync client.
- Stage 6: `docs/architecture/V050_FUTURE_FEATURES.md` (public side) and
  `openshard-cloud/docs/ROADMAP.md` (hosted side) for A–H.

## Partially completed

- Producers do not yet write the new v2 blocks (`actors`, `permissions`,
  `policy`, `approval`, `verifiers`, ...). The contract derives what it can
  from v0.4.3 fields; fixtures show the full shape. Wiring producers is the
  first item in the future-features doc.
- Dashboard is a functional shell (no pagination, no search, one org view
  per user across all memberships, no RBAC beyond membership).

## Blocked

- Creating the private GitHub repository `openshard-cloud` failed with
  HTTP 403 for both `orgs/openshard` and the personal account: this
  session's GitHub integration cannot create repositories. The repo exists
  as a local git repository with its history; it is delivered as a git
  bundle and tarball (see OVERNIGHT_SUMMARY.md) to be pushed to a private
  repository you create. Nothing from it was pushed to the public repo.

## Tests

- Public repo baseline (before changes): 8834 passed, 3 skipped.
- Public repo after Stage 1: 8877 passed, 3 skipped. Final run recorded in
  OVERNIGHT_SUMMARY.md.
- openshard-cloud: 16 passed (ingest, API, dashboard, live e2e).
- Lint (ruff) and mypy clean in both repos.

## Architecture decisions

1. Receipt state is derived, never stored, from explicit evidence in a
   fixed priority order (deny/denied → pending approval → failed → passed
   with escalation/retry → approved-not-verified → unverified).
2. All v0.5 record fields are optional and additive; `schema_version` stays
   `"1.2"`; the contract carries its own version `"2.0"`.
3. Outcomes are recorded beside the run record so the content hash stays
   valid; the contract takes them as an overlay.
4. Sync sends only the two existing privacy projections; the server stores
   the client's contract verbatim and never re-derives state.
5. The sync token comes from the environment only; HTTPS only except
   loopback; sync never runs on a hook path.
6. Cloud stack is Python (FastAPI/SQLAlchemy/Jinja2) to share the receipt
   code path and keep the first slice free of a JS build.
7. Approver, owner, requester and executor are separate principals; none is
   ever inferred from another.

## Files changed (public repo)

- New: `openshard/history/receipt_contract.py`, `openshard/history/outcomes.py`,
  `openshard/contracts/*`, `openshard/sync/*`, `tests/test_receipt_contract.py`,
  `tests/test_contracts.py`, `tests/test_sync_client.py`,
  `tests/fixtures/receipts/v2/*.json`, `docs/architecture/*.md`, `docs/sync.md`.
- Modified: `openshard/history/shard_contract.py` (state fields, State row,
  RECEIPT STATE block), `openshard/history/views.py` (extended `state`),
  `openshard/cli/main.py` (`receipt_contract` in `last --json`, RECEIPT
  ANSWERS in `last --full`, `outcome` and `sync` groups, form-factor
  hardening), `CHANGELOG.md`.

## Commits (public repo)

See `git log main..claude/openshard-v050-foundation-bn9gfk`.

## Things to review

- State priority order in `derive_receipt_state` (policy deny beats a granted
  approval; approval does not count as verification).
- The v2 record block names (`actors`, `permissions`, `policy`, `approval`,
  `verifiers`, `escalation`, `cost_breakdown`, `outcome`, `attestation`).
- The compact receipt shows a `State` row only for records with a v2 block.
- Sync envelope contents (`docs/sync.md`) and the token/endpoint rules.
- Cloud model column choices (string UUID keys, JSON contract columns).

## Recommended next steps

See OVERNIGHT_SUMMARY.md.
