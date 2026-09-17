import { Link, useParams } from "react-router-dom";
import type { Attempt, Receipt, Task } from "../api/types";
import {
  absoluteTime,
  changedFilesDisplay,
  cost,
  duration,
  evidenceKinds,
  gapsDisplay,
  modelDisplay,
  relativeTime,
  statusLabel,
  tokens,
} from "../lib/format";
import { useLoad } from "../lib/useApi";
import {
  CapturePill,
  ChecksText,
  Crumbs,
  ErrorState,
  Fact,
  IntegrityPill,
  LoadingState,
  Muted,
  NotFound,
  Rows,
  Section,
  StatusPill,
} from "../components/ui";

export function TaskPage() {
  const { taskId = "" } = useParams();
  const task = useLoad(`task:${taskId}`, (api) => api.getTask(taskId));

  if (task.state === "loading") return <LoadingState what="task" />;
  if (task.state === "error") return <ErrorState message={task.message} />;
  if (!task.data) return <NotFound what="task" id={taskId} />;
  return <TaskView task={task.data} />;
}

function TaskView({ task }: { task: Task }) {
  const latest = task.latest_receipt;
  const started = task.attempts[0]?.started_at ?? task.created_at;

  return (
    <>
      <Crumbs items={[{ to: "/", label: "Recent work" }, { label: task.title }]} />
      <div className="page-head">
        <h1>{task.title}</h1>
        <div className="badges">
          <StatusPill status={task.status} />
          <IntegrityPill integrity={latest.integrity} short />
          <CapturePill depth={latest.capture_completeness.depth} />
          <span className="muted" title={absoluteTime(task.updated_at)}>
            updated {relativeTime(task.updated_at)}
          </span>
        </div>
      </div>

      <div className="stack">
        <div className="facts">
          <Fact label="Repository">{task.repo}</Fact>
          <Fact label="Agent">{task.agent}</Fact>
          <Fact label="Model">{modelDisplay(task.model)}</Fact>
          <Fact label="Attempts">{task.attempt_count}</Fact>
          <Fact label="Latest duration">{duration(latest.duration_seconds)}</Fact>
          <Fact label="Started">
            <span title={absoluteTime(started)}>{relativeTime(started)}</span>
          </Fact>
          <Fact label="Last synced">
            <span title={absoluteTime(latest.synced_at)}>{relativeTime(latest.synced_at)}</span>
          </Fact>
          <Fact label="Branch">{latest.branch ?? <Muted>not recorded</Muted>}</Fact>
        </div>

        {task.task_full !== task.title ? (
          <Section title="Task">
            <p>{task.task_full}</p>
          </Section>
        ) : null}

        <Section title="Attempts" aside={task.attempt_count === 1 ? "one attempt" : `${task.attempt_count} attempts, oldest first`}>
          <div className="attempts" style={{ margin: "-12px -16px" }}>
            {task.attempts.map((a) => (
              <AttemptRow key={a.receipt_id} attempt={a} isLatest={a.receipt_id === task.latest_receipt_id} />
            ))}
          </div>
        </Section>

        <LatestReceipt receipt={latest} />
      </div>
    </>
  );
}

function AttemptRow({ attempt, isLatest }: { attempt: Attempt; isLatest: boolean }) {
  return (
    <Link to={`/receipts/${attempt.receipt_id}`} className="attempt">
      <span className="num">#{attempt.number}</span>
      <StatusPill status={attempt.status} />
      <span className="detail">
        <ChecksText verification={attempt.verification_status} checks={attempt.checks} />
        {" · "}
        <span title={absoluteTime(attempt.started_at)}>{relativeTime(attempt.started_at)}</span>
        {" · "}
        {duration(attempt.duration_seconds)}
      </span>
      <span className="link">{isLatest ? "Latest receipt →" : "Receipt →"}</span>
    </Link>
  );
}

function LatestReceipt({ receipt }: { receipt: Receipt }) {
  const evidence = evidenceKinds(receipt.evidence.kinds);
  return (
    <Section
      title="Latest receipt"
      aside={
        <Link to={`/receipts/${receipt.receipt_id}`} className="link" style={{ color: "var(--accent)" }}>
          Open full receipt →
        </Link>
      }
    >
      <Rows
        items={[
          ["Status", <>{receipt.task_completion ?? statusLabel(receipt.status)}</>],
          ["Files changed", changedFilesDisplay(receipt)],
          ["Checks", <ChecksText verification={receipt.verification_status} checks={receipt.checks} />],
          ["Integrity", <IntegrityPill integrity={receipt.integrity} />],
          ["Capture", <>{receipt.capture_completeness.depth} <Muted>· gaps: {gapsDisplay(receipt.capture_completeness)}</Muted></>],
          ["Cost", receipt.cost_usd === null ? <Muted>not recorded</Muted> : cost(receipt.cost_usd)],
          ["Tokens", receipt.tokens_input === null && receipt.tokens_output === null ? <Muted>not recorded</Muted> : tokens(receipt)],
          ["Evidence", evidence.length ? evidence.join(", ") : <Muted>none recorded</Muted>],
          ["Receipt ID", <code>{receipt.receipt_id}</code>],
        ]}
      />
    </Section>
  );
}
