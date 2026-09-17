/**
 * Dashboard data contract.
 *
 * `Receipt` mirrors `receipt_to_dict(extended=True)` in
 * openshard/history/views.py -- the privacy-bounded shape the CLI and MCP
 * server already emit -- plus the two fields hosted sync adds
 * (`synced_at`, `task_id`). `Task` is the hosted-side grouping of one or
 * more attempts; the local layer does not yet give a task a stable identity
 * across retries (see openshard/history/shard.py), so the server owns it.
 *
 * Rule carried over from the CLI: a value that was not captured is `null`,
 * never a zero or an empty string, and the UI says "not recorded".
 */

export type Agent = "Claude Code" | "Codex" | "Cursor" | "OpenCode" | "OpenShard";

/** Task-level outcome. "Completed" is a finished turn, not proof of correctness. */
export type TaskStatus = "completed" | "failed" | "in_progress";

/** Per-attempt outcome, same vocabulary as the task. */
export type AttemptStatus = TaskStatus;

export type VerificationStatus = "passed" | "failed" | "not_run" | "skipped" | "manual_review" | "unknown";

/** history/shard_hash.verify_shard_hash -> integrity_display. Never "signed". */
export type Integrity = "Matches (content hash)" | "Mismatch (content hash)" | "Not recorded";

export type CaptureDepth = "full" | "partial" | "unknown";
export type CompletenessStatus = "complete" | "incomplete" | "unknown";

export type Origin = "openshard_routed" | "external_observed" | "unknown";

export type EvidenceKind = "independently_verified" | "directly_observed" | "git_observed" | "agent_reported";

export type ChangeType = "create" | "update" | "delete";

/** v0.4.4 change provenance (adapters/claude_hooks._classify_changed_files). */
export type Attribution = "agent_reported" | "git_observed" | "pre_existing" | "other_session";

export type FindingSeverity = "Critical" | "High" | "Medium" | "Low" | "Note";

export interface CaptureCompleteness {
  depth: CaptureDepth;
  status: CompletenessStatus;
  reasons: { kind: string; count: number; detail: string | null }[];
  derived: boolean;
}

export interface FileChange {
  path: string;
  change_type: ChangeType | null;
  summary: string | null;
  attribution: Attribution | null;
}

export interface ChangesSummary {
  agent_reported: number;
  git_observed: number;
  pre_existing_excluded: number;
  other_session_excluded: number;
}

export interface CheckResult {
  name: string;
  status: "passed" | "failed" | "skipped";
  detail: string | null;
  duration_seconds: number | null;
}

export interface Finding {
  severity: FindingSeverity;
  message: string;
  path: string | null;
  line: number | null;
}

/** Bounded event summary: counts per evidence kind, plus tool activity. */
export interface EvidenceSummary {
  kinds: EvidenceKind[];
  tool_activity: { tool: string; count: number }[];
}

export interface Receipt {
  receipt_id: string;
  shard_id: string;
  task_id: string;
  run_id: string;
  attempt_number: number | null;

  task_short: string;
  task_full: string;

  agent: Agent;
  origin: Origin;
  model: string | null;
  strategy: string | null;

  status: AttemptStatus;
  /** Claude Code / OpenCode turn signal, e.g. "Turn completed (unverified)". Not verification. */
  task_completion: string | null;
  verification_status: VerificationStatus;
  verification_reason: string | null;
  result: string | null;
  error_class: string | null;

  repo: string;
  branch: string | null;
  git_head_commit_hash: string | null;
  git_base_commit_hash: string | null;
  git_dirty: boolean | null;

  files_changed: number;
  files: FileChange[];
  files_excluded: FileChange[];
  changes: ChangesSummary | null;
  diff_added: number | null;
  diff_removed: number | null;
  files_read_count: number | null;

  checks: string;
  check_results: CheckResult[];

  cost_usd: number | null;
  cost_provenance: "provider_reported" | "openshard_calculated" | "openshard_estimated" | null;
  tokens_input: number | null;
  tokens_output: number | null;
  tokens_cache_read: number | null;
  tokens_cache_creation: number | null;
  tokens_provenance: string | null;

  evidence: EvidenceSummary;
  findings: Finding[];
  capture_completeness: CaptureCompleteness;
  integrity: Integrity;

  created_at: string;
  duration_seconds: number | null;
  synced_at: string;
}

export interface Attempt {
  number: number;
  receipt_id: string;
  status: AttemptStatus;
  verification_status: VerificationStatus;
  checks: string;
  started_at: string;
  duration_seconds: number | null;
}

export interface TaskSummary {
  task_id: string;
  title: string;
  repo: string;
  agent: Agent;
  model: string | null;
  status: TaskStatus;
  attempt_count: number;
  latest_receipt_id: string;
  files_changed: number;
  checks: string;
  integrity: Integrity;
  capture_depth: CaptureDepth;
  created_at: string;
  updated_at: string;
}

export interface Task extends TaskSummary {
  task_full: string;
  attempts: Attempt[];
  latest_receipt: Receipt;
}
