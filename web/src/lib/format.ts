/**
 * Display helpers. Wording follows openshard/cli/visibility.py and
 * openshard/history/shard_contract.py so the dashboard and `openshard last`
 * say the same thing about the same receipt.
 */
import type { Attribution, CaptureCompleteness, EvidenceKind, Receipt, TaskStatus } from "../api/types";

export const NOT_RECORDED = "not recorded";

export function statusLabel(status: TaskStatus): string {
  switch (status) {
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "in_progress":
      return "In progress";
  }
}

export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "unknown time";
  const diffSec = Math.max(0, Math.round((now - then) / 1000));
  if (diffSec < 60) return "just now";
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min}m ago`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 30) return `${days}d ago`;
  return absoluteDate(iso);
}

/** `2026-09-17 14:03 UTC`; unparsable input is echoed, like fmt_time. */
export function absoluteTime(iso: string | null): string {
  if (!iso) return "unknown time";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:${p(d.getUTCMinutes())} UTC`;
}

export function absoluteDate(iso: string): string {
  return absoluteTime(iso).slice(0, 10);
}

/** Matches visibility.fmt_duration: `12.3s`, `3m 34s`, `1h 05m`. */
export function duration(seconds: number | null): string {
  if (seconds === null) return NOT_RECORDED;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return `${minutes}m ${String(secs).padStart(2, "0")}s`;
}

/** Every cost OpenShard knows is an estimate; the marker is never dropped. */
export function cost(usd: number | null): string {
  if (usd === null) return NOT_RECORDED;
  return `$${usd.toFixed(2)} est.`;
}

export const COST_PROVENANCE: Record<NonNullable<Receipt["cost_provenance"]>, string> = {
  provider_reported: "agent-reported",
  openshard_calculated: "tokens × list price",
  openshard_estimated: "OpenShard estimate",
};

export function tokenCount(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(1)}k`;
  return `${(n / 1_000_000).toFixed(2)}M`;
}

export function tokens(r: Pick<Receipt, "tokens_input" | "tokens_output" | "tokens_cache_read">): string {
  if (r.tokens_input === null && r.tokens_output === null) return NOT_RECORDED;
  let text = `${tokenCount(r.tokens_input ?? 0)} input / ${tokenCount(r.tokens_output ?? 0)} output`;
  if (r.tokens_cache_read) text += ` (+${tokenCount(r.tokens_cache_read)} cache read)`;
  return text;
}

export function filesChanged(n: number): string {
  return `${n} file${n === 1 ? "" : "s"}`;
}

export function diffStat(added: number | null, removed: number | null): string | null {
  if (added === null || removed === null) return null;
  return `+${added} / -${removed}`;
}

export const EVIDENCE_LABEL: Record<EvidenceKind, string> = {
  independently_verified: "Independently verified",
  directly_observed: "Directly observed",
  git_observed: "Git observed",
  agent_reported: "Agent reported",
};

const EVIDENCE_ORDER: EvidenceKind[] = ["independently_verified", "directly_observed", "git_observed", "agent_reported"];

export function evidenceKinds(kinds: EvidenceKind[]): string[] {
  return EVIDENCE_ORDER.filter((k) => kinds.includes(k)).map((k) => EVIDENCE_LABEL[k]);
}

export const ATTRIBUTION_TAG: Record<Attribution, string> = {
  agent_reported: "agent-reported",
  git_observed: "git-observed",
  pre_existing: "pre-existing",
  other_session: "other session",
};

export const CHANGE_LETTER = { create: "A", update: "M", delete: "D" } as const;

/** `2 files (1 agent-reported; 1 git-observed, actor not established)` like changed_files_display. */
export function changedFilesDisplay(r: Pick<Receipt, "files_changed" | "changes">): string {
  let text = filesChanged(r.files_changed);
  if (r.changes && r.files_changed > 0) {
    const parts: string[] = [];
    if (r.changes.agent_reported) parts.push(`${r.changes.agent_reported} agent-reported`);
    if (r.changes.git_observed) parts.push(`${r.changes.git_observed} git-observed, actor not established`);
    if (parts.length) text += ` (${parts.join("; ")})`;
  }
  return text;
}

/** Matches capture_completeness.gaps_display. */
export function gapsDisplay(block: CaptureCompleteness): string {
  if (block.status === "incomplete") {
    const details = block.reasons.slice(0, 3).map((r) => r.detail || r.kind);
    return details.join("; ") || "evidence known lost";
  }
  if (block.status === "complete") return "None known";
  return block.derived ? "Unknown (record predates loss tracking)" : "Unknown";
}

export function captureDepthDisplay(r: Pick<Receipt, "capture_completeness" | "origin">): string {
  const depth = r.capture_completeness.depth;
  return r.origin === "external_observed" ? `${depth} — OpenShard did not execute or verify this run` : depth;
}

export function shortHash(hash: string | null): string {
  return hash ? hash.slice(0, 7) : NOT_RECORDED;
}

export function modelDisplay(model: string | null): string {
  return model && model.trim() ? model : "unknown";
}
