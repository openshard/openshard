/**
 * Fixture data for the v0.5.0 dashboard.
 *
 * Every receipt here is shaped exactly like a synced `receipt_to_dict`
 * record. The set is chosen to exercise the states the UI must make
 * obvious: retries (failed then completed), a failed run, an in-progress
 * run, partial capture, a legacy record with no integrity hash, a hash
 * mismatch, checks not run, and cost/tokens present vs. not recorded.
 *
 * Task identity here is explicit: each Receipt carries the `task_id` Core
 * established when the task was created (`task_` + UUIDv7), and a fixture
 * Task is simply the Receipts that carry the same id. Nothing is grouped
 * by prompt, time, repo, agent or similarity. One Receipt (`r6`) predates
 * `task_id` and stays ungrouped on purpose.
 *
 * Timestamps are relative to load time so the history reads "2m ago" the
 * way the product brief describes it.
 */
import type { Attempt, Receipt, Task, TaskSummary } from "./types";

const NOW = Date.now();
const MIN = 60_000;

function ago(minutes: number, offsetSeconds = 0): string {
  return new Date(NOW - minutes * MIN + offsetSeconds * 1000).toISOString();
}

function shardId(iso: string, n: number): string {
  return `shard-${iso.slice(0, 10).replace(/-/g, "")}-${String(n).padStart(4, "0")}`;
}

type ReceiptSeed = Partial<Receipt> &
  Pick<Receipt, "receipt_id" | "task_short" | "agent" | "repo" | "created_at" | "status">;

/** Fill a receipt with the conservative defaults the CLI uses: nothing invented. */
function receipt(seed: ReceiptSeed, shardIndex: number): Receipt {
  const created = seed.created_at;
  const base: Receipt = {
    receipt_id: seed.receipt_id,
    shard_id: shardId(created, shardIndex),
    task_id: null,
    run_id: `run_${seed.receipt_id.slice(5, 17)}`,
    attempt_number: 1,
    task_short: seed.task_short,
    task_full: seed.task_short,
    agent: seed.agent,
    origin: "external_observed",
    model: null,
    strategy: null,
    status: seed.status,
    task_completion: null,
    verification_status: "unknown",
    verification_reason: null,
    result: null,
    error_class: null,
    repo: seed.repo,
    branch: null,
    git_head_commit_hash: null,
    git_base_commit_hash: null,
    git_dirty: null,
    files_changed: 0,
    files: [],
    files_excluded: [],
    changes: null,
    diff_added: null,
    diff_removed: null,
    files_read_count: null,
    checks: "Not run",
    check_results: [],
    cost_usd: null,
    cost_provenance: null,
    tokens_input: null,
    tokens_output: null,
    tokens_cache_read: null,
    tokens_cache_creation: null,
    tokens_provenance: null,
    evidence: { kinds: [], tool_activity: [] },
    findings: [],
    capture_completeness: { depth: "full", status: "complete", reasons: [], derived: false },
    integrity: "Matches (content hash)",
    created_at: created,
    duration_seconds: null,
    synced_at: new Date(new Date(created).getTime() + 4_000).toISOString(),
  };
  return { ...base, ...seed };
}

const checks = (names: string[], failed: string[] = []) =>
  names.map((name) => ({
    name,
    status: failed.includes(name) ? ("failed" as const) : ("passed" as const),
    detail: null,
    duration_seconds: null,
  }));

// ---------------------------------------------------------------------------
// Task 1: Fix refresh-token reuse bug -- OpenCode, retried once, completed.
// ---------------------------------------------------------------------------

const T1 = "task_019965a1-4d2e-7c3a-8f11-2b6e9d4a0c51";

const r1a = receipt(
  {
    receipt_id: "rcpt_3f0c9c2c7a1e4b77b0f2d6a5c9e11a01",
    task_id: T1,
    task_short: "Fix refresh-token reuse bug",
    task_full:
      "Fix refresh-token reuse bug: a rotated refresh token could still be redeemed once after rotation because the revocation write raced the token issue. Add a regression test.",
    agent: "OpenCode",
    repo: "session-auth-service",
    created_at: ago(9),
    status: "failed",
    attempt_number: 1,
    model: "GPT-5.6 Terra",
    task_completion: "Turn completed (unverified)",
    verification_status: "failed",
    verification_reason: "1 of 10 checks failed: test_refresh_rotation_race",
    result: "Moved revocation before issue; race still reproduced under the new test.",
    branch: "fix/refresh-token-reuse",
    git_head_commit_hash: "a41c9e2",
    git_base_commit_hash: "7d2f01b",
    git_dirty: true,
    files_changed: 2,
    files: [
      { path: "auth/refresh.py", change_type: "update", summary: "Revoke before issuing the replacement token", attribution: "agent_reported" },
      { path: "tests/test_refresh.py", change_type: "update", summary: "Add rotation race regression test", attribution: "agent_reported" },
    ],
    changes: { agent_reported: 2, git_observed: 0, pre_existing_excluded: 0, other_session_excluded: 0 },
    diff_added: 31,
    diff_removed: 6,
    files_read_count: 7,
    checks: "10 checks, 1 failed",
    check_results: checks(
      ["ruff", "mypy", "test_refresh_issue", "test_refresh_revoke", "test_refresh_rotation_race", "test_session_create", "test_session_expire", "test_login", "test_logout", "test_cookie_flags"],
      ["test_refresh_rotation_race"],
    ),
    cost_usd: 0.42,
    cost_provenance: "openshard_calculated",
    tokens_input: 48_210,
    tokens_output: 3_904,
    tokens_cache_read: 21_000,
    tokens_provenance: "opencode",
    evidence: {
      kinds: ["directly_observed", "git_observed", "agent_reported"],
      tool_activity: [
        { tool: "read", count: 7 },
        { tool: "edit", count: 3 },
        { tool: "bash", count: 4 },
      ],
    },
    duration_seconds: 214,
  },
  12,
);

const r1b = receipt(
  {
    receipt_id: "rcpt_8b2d5e6f1c3a4d9e8f7a6b5c4d3e2f10",
    task_id: T1,
    task_short: "Fix refresh-token reuse bug",
    task_full: r1a.task_full,
    agent: "OpenCode",
    repo: "session-auth-service",
    created_at: ago(2, -20),
    status: "completed",
    attempt_number: 2,
    model: "GPT-5.6 Terra",
    task_completion: "Turn completed (unverified)",
    verification_status: "passed",
    verification_reason: "10 of 10 checks passed",
    result: "Made revocation and issue a single transaction; regression test passes.",
    branch: "fix/refresh-token-reuse",
    git_head_commit_hash: "c93be07",
    git_base_commit_hash: "7d2f01b",
    git_dirty: false,
    files_changed: 1,
    files: [
      { path: "auth/refresh.py", change_type: "update", summary: "Wrap revoke + issue in one transaction", attribution: "agent_reported" },
    ],
    changes: { agent_reported: 1, git_observed: 0, pre_existing_excluded: 0, other_session_excluded: 0 },
    diff_added: 14,
    diff_removed: 9,
    files_read_count: 3,
    checks: "10 checks passed",
    check_results: checks(
      ["ruff", "mypy", "test_refresh_issue", "test_refresh_revoke", "test_refresh_rotation_race", "test_session_create", "test_session_expire", "test_login", "test_logout", "test_cookie_flags"],
    ),
    cost_usd: 0.19,
    cost_provenance: "openshard_calculated",
    tokens_input: 22_640,
    tokens_output: 1_812,
    tokens_cache_read: 18_400,
    tokens_provenance: "opencode",
    evidence: {
      kinds: ["directly_observed", "git_observed", "agent_reported"],
      tool_activity: [
        { tool: "read", count: 3 },
        { tool: "edit", count: 1 },
        { tool: "bash", count: 2 },
      ],
    },
    duration_seconds: 96,
  },
  13,
);

// ---------------------------------------------------------------------------
// Task 2: Add billing webhook -- Claude Code, one attempt, completed.
// ---------------------------------------------------------------------------

const T2 = "task_0199659e-88b0-7a12-9c4d-6e1f0a2b3c74";

const r2 = receipt(
  {
    receipt_id: "rcpt_5a7e1f2b9c8d4e3f2a1b0c9d8e7f6a52",
    task_id: T2,
    task_short: "Add billing webhook",
    task_full:
      "Add a Stripe billing webhook endpoint that verifies signatures, records invoice.paid and invoice.payment_failed, and is idempotent on event id.",
    agent: "Claude Code",
    repo: "billing-api",
    created_at: ago(21),
    status: "completed",
    model: "Claude Opus 5",
    task_completion: "Turn completed (unverified)",
    verification_status: "passed",
    verification_reason: "23 of 23 checks passed",
    result: "Webhook endpoint added with signature verification and idempotency table.",
    branch: "feat/billing-webhook",
    git_head_commit_hash: "1e8f4a0",
    git_base_commit_hash: "b06c2d9",
    git_dirty: false,
    files_changed: 4,
    files: [
      { path: "billing/webhooks.py", change_type: "create", summary: "Stripe webhook handler", attribution: "agent_reported" },
      { path: "billing/models.py", change_type: "update", summary: "WebhookEvent idempotency model", attribution: "agent_reported" },
      { path: "billing/urls.py", change_type: "update", summary: "Route /webhooks/stripe", attribution: "agent_reported" },
      { path: "tests/billing/test_webhooks.py", change_type: "create", summary: "Signature and idempotency tests", attribution: "agent_reported" },
    ],
    files_excluded: [
      { path: "README.md", change_type: "update", summary: null, attribution: "pre_existing" },
    ],
    changes: { agent_reported: 4, git_observed: 0, pre_existing_excluded: 1, other_session_excluded: 0 },
    diff_added: 212,
    diff_removed: 4,
    files_read_count: 11,
    checks: "23 checks passed",
    check_results: checks([
      "ruff", "mypy", "test_webhook_signature_valid", "test_webhook_signature_invalid", "test_webhook_replay_rejected",
      "test_invoice_paid", "test_invoice_payment_failed", "test_unknown_event_ignored", "test_idempotent_on_event_id",
      "test_models_migration", "test_urls", "test_customer_create", "test_customer_update", "test_subscription_create",
      "test_subscription_cancel", "test_invoice_list", "test_invoice_get", "test_plan_list", "test_plan_get",
      "test_health", "test_auth_required", "test_rate_limit", "test_cors",
    ]),
    cost_usd: 1.87,
    cost_provenance: "provider_reported",
    tokens_input: 184_300,
    tokens_output: 12_410,
    tokens_cache_read: 143_900,
    tokens_cache_creation: 9_800,
    tokens_provenance: "claude_code",
    evidence: {
      kinds: ["directly_observed", "git_observed", "agent_reported"],
      tool_activity: [
        { tool: "Read", count: 11 },
        { tool: "Edit", count: 5 },
        { tool: "Write", count: 2 },
        { tool: "Bash", count: 6 },
      ],
    },
    findings: [
      { severity: "Low", message: "Webhook secret read from env at import time; consider lazy lookup for tests.", path: "billing/webhooks.py", line: 14 },
    ],
    duration_seconds: 1_143,
  },
  40,
);

// ---------------------------------------------------------------------------
// Task 3: Refactor auth middleware -- Codex, one attempt, failed.
// ---------------------------------------------------------------------------

const T3 = "task_0199657c-1f60-7b9e-8d02-4a5b6c7d8e93";

const r3 = receipt(
  {
    receipt_id: "rcpt_9c1d2e3f4a5b6c7d8e9f0a1b2c3d4e73",
    task_id: T3,
    task_short: "Refactor auth middleware",
    task_full:
      "Refactor the auth middleware so session lookup and token validation are separate stages, without changing the public decorator API.",
    agent: "Codex",
    repo: "session-auth-service",
    created_at: ago(61),
    status: "failed",
    model: "GPT-5.5 Codex",
    task_completion: "Turn completed (unverified)",
    verification_status: "failed",
    verification_reason: "3 of 12 checks failed",
    result: "Split middleware into two stages; decorator ordering changed and broke role checks.",
    error_class: "verification_failed",
    branch: "refactor/auth-middleware",
    git_head_commit_hash: "f2a77c1",
    git_base_commit_hash: "7d2f01b",
    git_dirty: true,
    files_changed: 3,
    files: [
      { path: "auth/middleware.py", change_type: "update", summary: "Split into SessionStage and TokenStage", attribution: "agent_reported" },
      { path: "auth/decorators.py", change_type: "update", summary: "require_role reads stage output", attribution: "agent_reported" },
      { path: "auth/__init__.py", change_type: "update", summary: null, attribution: "git_observed" },
    ],
    changes: { agent_reported: 2, git_observed: 1, pre_existing_excluded: 0, other_session_excluded: 0 },
    diff_added: 88,
    diff_removed: 71,
    files_read_count: 9,
    checks: "12 checks, 3 failed",
    check_results: checks(
      ["ruff", "mypy", "test_middleware_session", "test_middleware_token", "test_require_auth", "test_require_role_admin",
        "test_require_role_member", "test_require_role_order", "test_login", "test_logout", "test_refresh_issue", "test_cookie_flags"],
      ["test_require_role_admin", "test_require_role_member", "test_require_role_order"],
    ),
    cost_usd: 0.66,
    cost_provenance: "openshard_estimated",
    tokens_input: 71_900,
    tokens_output: 6_120,
    tokens_provenance: "codex",
    evidence: {
      kinds: ["directly_observed", "git_observed", "agent_reported"],
      tool_activity: [
        { tool: "shell", count: 9 },
        { tool: "apply_patch", count: 4 },
      ],
    },
    findings: [
      { severity: "High", message: "require_role now runs before the session stage; role checks see an empty principal.", path: "auth/decorators.py", line: 42 },
    ],
    duration_seconds: 388,
  },
  11,
);

// ---------------------------------------------------------------------------
// Task 4: Migrate user avatars to S3 -- Cursor, partial capture, cost unknown.
// ---------------------------------------------------------------------------

const T4 = "task_01996520-9a44-7d31-b7e5-0c1d2e3f4a05";

const r4 = receipt(
  {
    receipt_id: "rcpt_2d4f6a8c0e1b3d5f7a9c1e3b5d7f9a24",
    task_id: T4,
    task_short: "Migrate user avatars to S3",
    task_full: "Move avatar uploads from local disk to S3 behind the existing storage interface, with a one-off migration command.",
    agent: "Cursor",
    repo: "web-app",
    created_at: ago(185),
    status: "completed",
    model: "Claude Sonnet 5",
    task_completion: "Turn completed (unverified)",
    verification_status: "passed",
    verification_reason: "8 of 8 checks passed",
    result: "S3 storage backend added; migration command copies existing files.",
    branch: "feat/avatars-s3",
    git_head_commit_hash: "9b31e5d",
    git_base_commit_hash: "44a0c8e",
    git_dirty: false,
    files_changed: 5,
    files: [
      { path: "storage/s3.py", change_type: "create", summary: "S3Storage backend", attribution: "agent_reported" },
      { path: "storage/__init__.py", change_type: "update", summary: "Select backend from settings", attribution: "agent_reported" },
      { path: "management/commands/migrate_avatars.py", change_type: "create", summary: null, attribution: "git_observed" },
      { path: "settings/base.py", change_type: "update", summary: null, attribution: "git_observed" },
      { path: "tests/test_storage_s3.py", change_type: "create", summary: null, attribution: "git_observed" },
    ],
    changes: { agent_reported: 2, git_observed: 3, pre_existing_excluded: 0, other_session_excluded: 0 },
    diff_added: 164,
    diff_removed: 12,
    checks: "8 checks passed",
    check_results: checks(["ruff", "test_s3_put", "test_s3_get", "test_s3_delete", "test_backend_select", "test_migrate_command", "test_avatar_upload", "test_avatar_url"]),
    evidence: {
      kinds: ["git_observed", "agent_reported"],
      tool_activity: [{ tool: "edit", count: 2 }],
    },
    capture_completeness: {
      depth: "partial",
      status: "incomplete",
      reasons: [{ kind: "hook_gap", count: 3, detail: "3 file edits were observed by git only; the Cursor hook did not report them" }],
      derived: false,
    },
    duration_seconds: 742,
  },
  6,
);

// ---------------------------------------------------------------------------
// Task 5: Investigate flaky checkout test -- Codex, in progress.
// ---------------------------------------------------------------------------

const T5 = "task_019965a0-2c18-7e5f-a3b8-9d0e1f2a3b46";

const r5 = receipt(
  {
    receipt_id: "rcpt_6e8a0c2e4f6b8d0f2a4c6e8b0d2f4a65",
    task_id: T5,
    task_short: "Investigate flaky checkout test",
    task_full: "Find out why test_checkout_total fails roughly one run in twenty on CI and fix the cause, not the test.",
    agent: "Codex",
    repo: "web-app",
    created_at: ago(6),
    status: "in_progress",
    model: "GPT-5.5 Codex",
    task_completion: "In progress",
    verification_status: "not_run",
    branch: "main",
    git_base_commit_hash: "44a0c8e",
    git_dirty: false,
    files_changed: 0,
    files_read_count: 14,
    checks: "Not run",
    evidence: {
      kinds: ["directly_observed", "agent_reported"],
      tool_activity: [{ tool: "shell", count: 12 }],
    },
    synced_at: ago(3),
  },
  7,
);

// ---------------------------------------------------------------------------
// Legacy Receipt: Bump dependencies and fix lint -- Claude Code, recorded
// before task_id and content hashing existed. Valid, but belongs to no Task.
// ---------------------------------------------------------------------------

const r6 = receipt(
  {
    receipt_id: "rcpt_1a3c5e7a9b1d3f5a7c9e1b3d5f7a9c16",
    attempt_number: null,
    task_short: "Bump dependencies and fix lint",
    task_full: "Bump dependencies and fix lint",
    agent: "Claude Code",
    repo: "billing-api",
    created_at: ago(26 * 60),
    status: "completed",
    task_completion: "Turn completed (unverified)",
    verification_status: "not_run",
    result: "Dependencies bumped; lint clean locally per agent.",
    branch: "chore/deps",
    files_changed: 2,
    files: [
      { path: "pyproject.toml", change_type: "update", summary: null, attribution: null },
      { path: "uv.lock", change_type: "update", summary: null, attribution: null },
    ],
    checks: "Not run",
    evidence: { kinds: ["agent_reported"], tool_activity: [{ tool: "Bash", count: 5 }, { tool: "Edit", count: 2 }] },
    capture_completeness: { depth: "full", status: "unknown", reasons: [], derived: true },
    integrity: "Not recorded",
    duration_seconds: 305,
  },
  3,
);

// ---------------------------------------------------------------------------
// Task 7: Add rate limiting to login -- OpenCode, hash mismatch.
// ---------------------------------------------------------------------------

const T7 = "task_01995c2e-7788-7f0a-9e6c-3b4c5d6e7f27";

const r7 = receipt(
  {
    receipt_id: "rcpt_7f9b1d3f5a7c9e1b3d5f7a9c1e3b5d77",
    task_id: T7,
    task_short: "Add rate limiting to login endpoint",
    task_full: "Add per-IP and per-account rate limiting to POST /login using the existing Redis client, with a 429 response and Retry-After header.",
    agent: "OpenCode",
    repo: "session-auth-service",
    created_at: ago(2 * 24 * 60 + 40),
    status: "completed",
    model: "GPT-5.6 Terra",
    task_completion: "Turn completed (unverified)",
    verification_status: "passed",
    verification_reason: "9 of 9 checks passed",
    result: "Rate limiter added on login with per-IP and per-account buckets.",
    branch: "feat/login-rate-limit",
    git_head_commit_hash: "5d0a9f3",
    git_base_commit_hash: "2c8e1b4",
    git_dirty: false,
    files_changed: 3,
    files: [
      { path: "auth/ratelimit.py", change_type: "create", summary: "Token bucket on Redis", attribution: "agent_reported" },
      { path: "auth/views.py", change_type: "update", summary: "Apply limiter to login", attribution: "agent_reported" },
      { path: "tests/test_ratelimit.py", change_type: "create", summary: null, attribution: "agent_reported" },
    ],
    changes: { agent_reported: 3, git_observed: 0, pre_existing_excluded: 0, other_session_excluded: 0 },
    diff_added: 121,
    diff_removed: 3,
    files_read_count: 6,
    checks: "9 checks passed",
    check_results: checks(["ruff", "mypy", "test_limit_ip", "test_limit_account", "test_retry_after", "test_login", "test_logout", "test_refresh_issue", "test_cookie_flags"]),
    cost_usd: 0.31,
    cost_provenance: "openshard_calculated",
    tokens_input: 39_800,
    tokens_output: 2_960,
    tokens_provenance: "opencode",
    evidence: {
      kinds: ["directly_observed", "git_observed", "agent_reported"],
      tool_activity: [{ tool: "read", count: 6 }, { tool: "edit", count: 3 }, { tool: "bash", count: 3 }],
    },
    integrity: "Mismatch (content hash)",
    duration_seconds: 267,
  },
  4,
);

// ---------------------------------------------------------------------------
// Task 8: Write ADR for event sourcing -- Claude Code, docs only, no checks.
// ---------------------------------------------------------------------------

const T8 = "task_0199581d-3c9a-7a6b-8f4e-5a6b7c8d9e08";

const r8 = receipt(
  {
    receipt_id: "rcpt_4b6d8f0a2c4e6a8c0e2a4c6e8a0c2e48",
    task_id: T8,
    task_short: "Write ADR for event sourcing",
    task_full: "Write an architecture decision record on adopting event sourcing for the ledger, covering alternatives considered and migration risk.",
    agent: "Claude Code",
    repo: "billing-api",
    created_at: ago(3 * 24 * 60 + 130),
    status: "completed",
    model: "Claude Opus 5",
    task_completion: "Turn completed (unverified)",
    verification_status: "not_run",
    result: "ADR-014 drafted with three alternatives and a phased migration.",
    branch: "docs/adr-014",
    git_head_commit_hash: "e71b2c6",
    git_base_commit_hash: "b06c2d9",
    git_dirty: false,
    files_changed: 1,
    files: [{ path: "docs/adr/014-event-sourcing.md", change_type: "create", summary: "ADR-014", attribution: "agent_reported" }],
    changes: { agent_reported: 1, git_observed: 0, pre_existing_excluded: 0, other_session_excluded: 0 },
    diff_added: 96,
    diff_removed: 0,
    files_read_count: 8,
    checks: "Not run",
    cost_usd: 0.74,
    cost_provenance: "provider_reported",
    tokens_input: 61_200,
    tokens_output: 4_880,
    tokens_cache_read: 40_100,
    tokens_provenance: "claude_code",
    evidence: {
      kinds: ["directly_observed", "git_observed", "agent_reported"],
      tool_activity: [{ tool: "Read", count: 8 }, { tool: "Write", count: 1 }],
    },
    duration_seconds: 421,
  },
  2,
);

// ---------------------------------------------------------------------------
// Tasks: one per explicit task_id. A fixture Task is only ever the Receipts
// that already carry that id -- never a grouping the dashboard worked out.
// ---------------------------------------------------------------------------

export const RECEIPTS: Receipt[] = [r1a, r1b, r2, r3, r4, r5, r6, r7, r8];

function attempt(r: Receipt): Attempt {
  return {
    number: r.attempt_number ?? 1,
    receipt_id: r.receipt_id,
    status: r.status,
    verification_status: r.verification_status,
    checks: r.checks,
    started_at: r.created_at,
    duration_seconds: r.duration_seconds,
  };
}

function task(taskId: string, title: string, receipts: Receipt[]): Task {
  for (const r of receipts) {
    if (r.task_id !== taskId) {
      throw new Error(`fixture receipt ${r.receipt_id} does not carry task_id ${taskId}`);
    }
  }
  const ordered = [...receipts].sort((a, b) => (a.attempt_number ?? 1) - (b.attempt_number ?? 1));
  const latest = ordered[ordered.length - 1];
  const first = ordered[0];
  return {
    task_id: taskId,
    title,
    task_full: latest.task_full,
    repo: latest.repo,
    agent: latest.agent,
    model: latest.model,
    status: latest.status,
    attempt_count: ordered.length,
    latest_receipt_id: latest.receipt_id,
    files_changed: latest.files_changed,
    checks: latest.checks,
    integrity: latest.integrity,
    capture_depth: latest.capture_completeness.depth,
    created_at: first.created_at,
    updated_at: latest.synced_at,
    attempts: ordered.map(attempt),
    latest_receipt: latest,
  };
}

export const TASKS: Task[] = [
  task(T1, "Fix refresh-token reuse bug", [r1a, r1b]),
  task(T5, "Investigate flaky checkout test", [r5]),
  task(T2, "Add billing webhook", [r2]),
  task(T3, "Refactor auth middleware", [r3]),
  task(T4, "Migrate user avatars to S3", [r4]),
  task(T7, "Add rate limiting to login endpoint", [r7]),
  task(T8, "Write ADR for event sourcing", [r8]),
];

export function toSummary(t: Task): TaskSummary {
  const { attempts: _a, latest_receipt: _r, task_full: _f, ...summary } = t;
  return summary;
}
