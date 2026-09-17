import { Link } from "react-router-dom";
import type { TaskSummary } from "../api/types";
import { absoluteTime, filesChanged, modelDisplay, relativeTime } from "../lib/format";
import { useLoad } from "../lib/useApi";
import { CapturePill, ErrorState, LoadingState, StatusPill } from "../components/ui";

export function HistoryPage() {
  const tasks = useLoad("tasks", (api) => api.listTasks());

  return (
    <>
      <div className="page-head">
        <h1>Recent work</h1>
        <div className="sub">
          {tasks.state === "ready"
            ? `Showing ${tasks.data.length} task${tasks.data.length === 1 ? "" : "s"}, newest first. Costs are estimates.`
            : "What your coding agents have done, newest first."}
        </div>
      </div>

      {tasks.state === "loading" ? <LoadingState what="recent work" /> : null}
      {tasks.state === "error" ? <ErrorState message={tasks.message} /> : null}
      {tasks.state === "ready" ? (
        tasks.data.length === 0 ? (
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
            {tasks.data.map((t) => (
              <HistoryRow key={t.task_id} task={t} />
            ))}
          </div>
        )
      ) : null}
    </>
  );
}

function HistoryRow({ task }: { task: TaskSummary }) {
  return (
    <Link to={`/tasks/${task.task_id}`} className="history-row">
      <div className="cell c-title">
        <div className="title">
          <span>{task.title}</span>
          {task.attempt_count > 1 ? <span className="tag">{task.attempt_count} attempts</span> : null}
          <CapturePill depth={task.capture_depth} />
        </div>
        <div className="meta">
          {task.repo} · {modelDisplay(task.model)}
        </div>
      </div>
      <div className="cell c-agent">{task.agent}</div>
      <div className="cell c-status">
        <StatusPill status={task.status} />
        <div className="sub">{task.checks === "Not run" ? "checks not run" : task.checks.toLowerCase()}</div>
      </div>
      <div className="cell c-files num">{task.files_changed ? filesChanged(task.files_changed) : "—"}</div>
      <div className="cell c-time right" title={absoluteTime(task.updated_at)}>
        {relativeTime(task.updated_at)}
      </div>
    </Link>
  );
}
