import { Link } from "react-router-dom";
import type { Agent, CaptureDepth, TaskStatus, WorkItem } from "../api/types";
import { absoluteTime, filesChanged, modelDisplay, relativeTime } from "../lib/format";
import { useLoad } from "../lib/useApi";
import { CapturePill, ErrorState, LoadingState, StatusPill } from "../components/ui";

export function HistoryPage() {
  const work = useLoad("work", (api) => api.listWork());

  return (
    <>
      <div className="page-head">
        <h1>Recent work</h1>
        <div className="sub">
          {work.state === "ready"
            ? `Showing ${work.data.length} item${work.data.length === 1 ? "" : "s"}, newest first. Costs are estimates.`
            : "What your coding agents have done, newest first."}
        </div>
      </div>

      {work.state === "loading" ? <LoadingState what="recent work" /> : null}
      {work.state === "error" ? <ErrorState message={work.message} /> : null}
      {work.state === "ready" ? (
        work.data.length === 0 ? (
          <div className="state">No receipts have synced yet. Use your coding agent normally and come back.</div>
        ) : (
          <div className="history">
            <div className="history-head">
              <span>Task</span>
              <span>Agent</span>
              <span>Status</span>
              <span>Files</span>
              <span style={{ textAlign: "right" }}>Updated</span>
            </div>
            {work.data.map((item) => (
              <HistoryRow key={rowKey(item)} row={toRow(item)} />
            ))}
          </div>
        )
      ) : null}
    </>
  );
}

/** The one row shape both kinds of work item render through. */
interface Row {
  to: string;
  title: string;
  /** "N attempts" on a retried Task; "No task" on a standalone Receipt; null otherwise. */
  tag: string | null;
  repo: string;
  model: string | null;
  agent: Agent;
  status: TaskStatus;
  checks: string;
  files_changed: number;
  capture_depth: CaptureDepth;
  updated_at: string;
}

function rowKey(item: WorkItem): string {
  return item.kind === "task" ? item.task.task_id : item.receipt.receipt_id;
}

function toRow(item: WorkItem): Row {
  if (item.kind === "task") {
    const t = item.task;
    return {
      to: `/tasks/${t.task_id}`,
      title: t.title,
      tag: t.attempt_count > 1 ? `${t.attempt_count} attempts` : null,
      repo: t.repo,
      model: t.model,
      agent: t.agent,
      status: t.status,
      checks: t.checks,
      files_changed: t.files_changed,
      capture_depth: t.capture_depth,
      updated_at: t.updated_at,
    };
  }
  const r = item.receipt;
  return {
    to: `/receipts/${r.receipt_id}`,
    title: r.title,
    tag: "No task",
    repo: r.repo,
    model: r.model,
    agent: r.agent,
    status: r.status,
    checks: r.checks,
    files_changed: r.files_changed,
    capture_depth: r.capture_depth,
    updated_at: r.updated_at,
  };
}

function HistoryRow({ row }: { row: Row }) {
  return (
    <Link to={row.to} className="history-row">
      <div className="cell c-title">
        <div className="title">
          <span>{row.title}</span>
          {row.tag ? <span className="tag">{row.tag}</span> : null}
          <CapturePill depth={row.capture_depth} />
        </div>
        <div className="meta">
          {row.repo} · {modelDisplay(row.model)}
        </div>
      </div>
      <div className="cell c-agent">{row.agent}</div>
      <div className="cell c-status">
        <StatusPill status={row.status} />
        <div className="sub">{row.checks === "Not run" ? "checks not run" : row.checks.toLowerCase()}</div>
      </div>
      <div className="cell c-files num">{row.files_changed ? filesChanged(row.files_changed) : "—"}</div>
      <div className="cell c-time right" title={absoluteTime(row.updated_at)}>
        {relativeTime(row.updated_at)}
      </div>
    </Link>
  );
}
