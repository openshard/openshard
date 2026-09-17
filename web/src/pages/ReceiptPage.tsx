import { Link, useParams } from "react-router-dom";
import type { Receipt } from "../api/types";
import {
  ATTRIBUTION_TAG,
  CHANGE_LETTER,
  COST_PROVENANCE,
  absoluteTime,
  captureDepthDisplay,
  changedFilesDisplay,
  cost,
  diffStat,
  duration,
  evidenceKinds,
  gapsDisplay,
  modelDisplay,
  relativeTime,
  shortHash,
  statusLabel,
  tokenCount,
  tokens,
} from "../lib/format";
import { useLoad } from "../lib/useApi";
import {
  CapturePill,
  ChecksText,
  Crumbs,
  ErrorState,
  IntegrityPill,
  LoadingState,
  Muted,
  NotFound,
  Rows,
  Section,
  StatusPill,
} from "../components/ui";

export function ReceiptPage() {
  const { receiptId = "" } = useParams();
  const receipt = useLoad(`receipt:${receiptId}`, (api) => api.getReceipt(receiptId));

  if (receipt.state === "loading") return <LoadingState what="receipt" />;
  if (receipt.state === "error") return <ErrorState message={receipt.message} />;
  if (!receipt.data) return <NotFound what="receipt" id={receiptId} />;
  return <ReceiptView r={receipt.data} />;
}

function ReceiptView({ r }: { r: Receipt }) {
  const evidence = evidenceKinds(r.evidence.kinds);
  const failed = r.check_results.filter((c) => c.status === "failed").length;
  const cc = r.capture_completeness;

  return (
    <>
      <Crumbs
        items={[
          { to: "/", label: "Recent work" },
          { to: `/tasks/${r.task_id}`, label: r.task_short },
          { label: `Receipt${r.attempt_number ? ` · attempt #${r.attempt_number}` : ""}` },
        ]}
      />
      <div className="page-head">
        <h1>{r.task_short}</h1>
        <div className="badges">
          <StatusPill status={r.status} />
          <IntegrityPill integrity={r.integrity} />
          <CapturePill depth={cc.depth} />
          <code className="muted">{r.receipt_id}</code>
        </div>
      </div>

      <div className="stack">
        {r.task_full !== r.task_short ? (
          <Section title="Task">
            <p>{r.task_full}</p>
          </Section>
        ) : null}

        <Section title="Execution">
          <Rows
            items={[
              ["Executor", <>{r.agent}{r.origin === "external_observed" ? <Muted> (external)</Muted> : null}</>],
              ["Model", modelDisplay(r.model)],
              ["Status", r.task_completion ?? statusLabel(r.status)],
              ["Verification", <><ChecksText verification={r.verification_status} checks={r.checks} />{r.verification_reason && r.verification_status !== "passed" ? <span className="hint">{r.verification_reason}</span> : null}</>],
              ["Result", r.result ?? <Muted>not recorded</Muted>],
              ["Duration", duration(r.duration_seconds)],
              ["Attempt", r.attempt_number !== null ? <>#{r.attempt_number} <Link to={`/tasks/${r.task_id}`} style={{ color: "var(--accent)" }}>· view task</Link></> : <Muted>not tracked</Muted>],
              ...(r.error_class ? ([["Error class", <code>{r.error_class}</code>]] as [string, React.ReactNode][]) : []),
            ]}
          />
        </Section>

        <Section title="Repository">
          <Rows
            items={[
              ["Repo", r.repo],
              ["Branch", r.branch ?? <Muted>not recorded</Muted>],
              ["Head commit", r.git_head_commit_hash ? <code>{shortHash(r.git_head_commit_hash)}</code> : <Muted>not recorded</Muted>],
              ["Base commit", r.git_base_commit_hash ? <code>{shortHash(r.git_base_commit_hash)}</code> : <Muted>not recorded</Muted>],
              ["Dirty", r.git_dirty === null ? <Muted>not recorded</Muted> : r.git_dirty ? "yes" : "no"],
              ["Files read", r.files_read_count === null ? <Muted>not recorded</Muted> : `${r.files_read_count} file${r.files_read_count === 1 ? "" : "s"}`],
            ]}
          />
        </Section>

        <Section
          title="Changes"
          aside={
            <>
              {changedFilesDisplay(r)}
              {diffStat(r.diff_added, r.diff_removed) ? ` · ${diffStat(r.diff_added, r.diff_removed)}` : ""}
            </>
          }
        >
          {r.files.length ? (
            <ul className="filelist">
              {r.files.map((f) => (
                <FileLine key={f.path} f={f} />
              ))}
            </ul>
          ) : (
            <p className="muted">No file changes recorded.</p>
          )}
          {r.files_excluded.length ? (
            <>
              <p className="note" style={{ marginTop: 12, marginBottom: 4 }}>
                Not counted — git showed these, but they are not this run's work:
              </p>
              <ul className="filelist excluded">
                {r.files_excluded.map((f) => (
                  <FileLine key={f.path} f={f} />
                ))}
              </ul>
            </>
          ) : null}
        </Section>

        <Section
          title="Checks"
          aside={r.check_results.length ? `${r.check_results.length} run · ${failed ? `${failed} failed` : "all passed"}` : undefined}
        >
          {r.check_results.length ? (
            <ul className="checklist">
              {[...r.check_results]
                .sort((a, b) => (a.status === "failed" ? -1 : 0) - (b.status === "failed" ? -1 : 0))
                .map((c) => (
                  <li key={c.name} className={c.status}>
                    <span className={`mark ${c.status}`}>{c.status === "passed" ? "✓" : c.status === "failed" ? "✖" : "–"}</span>
                    <span title={c.detail ?? undefined}>{c.name}</span>
                  </li>
                ))}
            </ul>
          ) : (
            <p className="muted">
              {r.checks === "Not run" ? "No checks were run. A completed turn is not proof the code is correct." : r.checks}
            </p>
          )}
        </Section>

        <Section title="Cost and tokens">
          <Rows
            items={[
              ["Cost", r.cost_usd === null ? <Muted>not recorded</Muted> : <>{cost(r.cost_usd)}{r.cost_provenance ? <span className="hint">{COST_PROVENANCE[r.cost_provenance]}</span> : null}</>],
              ["Tokens", r.tokens_input === null && r.tokens_output === null ? <Muted>not recorded</Muted> : <>{tokens(r)}{r.tokens_provenance ? <span className="hint">reported by {r.tokens_provenance}</span> : null}</>],
              ...(r.tokens_cache_creation ? ([["Cache written", tokenCount(r.tokens_cache_creation)]] as [string, React.ReactNode][]) : []),
            ]}
          />
          <p className="note" style={{ marginTop: 10 }}>
            Every cost OpenShard shows is an estimate. Agent-reported figures are approximate; OpenShard's own figure is tokens × list price.
          </p>
        </Section>

        <Section title="Evidence" aside={evidence.length ? evidence.join(", ") : undefined}>
          <Rows
            items={[
              ["Evidence kinds", evidence.length ? evidence.join(", ") : <Muted>none recorded</Muted>],
              [
                "Activity",
                r.evidence.tool_activity.length ? (
                  <span className="mono">{r.evidence.tool_activity.map((t) => `${t.tool} × ${t.count}`).join("   ")}</span>
                ) : (
                  <Muted>none recorded</Muted>
                ),
              ],
            ]}
          />
          {r.findings.length ? (
            <ul className="findings" style={{ marginTop: 12 }}>
              {r.findings.map((f, i) => (
                <li key={i}>
                  <span className={`sev ${f.severity}`}>{f.severity}</span>
                  <span>
                    {f.message}
                    {f.path ? <span className="where">{f.path}{f.line !== null ? `:${f.line}` : ""}</span> : null}
                  </span>
                </li>
              ))}
            </ul>
          ) : null}
        </Section>

        <Section title="Capture">
          <Rows
            items={[
              ["Capture depth", captureDepthDisplay(r)],
              ["Completeness", cc.status.charAt(0).toUpperCase() + cc.status.slice(1)],
              ["Known gaps", gapsDisplay(cc)],
            ]}
          />
        </Section>

        <Section title="Integrity">
          <Rows
            items={[
              ["Content hash", <IntegrityPill integrity={r.integrity} />],
              ["Receipt ID", <code>{r.receipt_id}</code>],
              ["Shard ID", <><code>{r.shard_id}</code><span className="hint">position in the repo's local history</span></>],
              ["Run ID", <code>{r.run_id}</code>],
            ]}
          />
          <p className="note" style={{ marginTop: 10 }}>{integrityNote(r.integrity)}</p>
        </Section>

        <Section title="Timestamps">
          <Rows
            items={[
              ["Recorded", <>{absoluteTime(r.created_at)} <Muted>· {relativeTime(r.created_at)}</Muted></>],
              ["Duration", duration(r.duration_seconds)],
              ["Synced", <>{absoluteTime(r.synced_at)} <Muted>· {relativeTime(r.synced_at)}</Muted></>],
            ]}
          />
        </Section>
      </div>
    </>
  );
}

function FileLine({ f }: { f: Receipt["files"][number] }) {
  const letter = f.change_type ? CHANGE_LETTER[f.change_type] : "M";
  return (
    <li>
      <span className={`letter ${letter}`}>{letter}</span>
      <span>
        <span className="path">{f.path}</span>
        {f.summary ? <span className="summary">{f.summary}</span> : null}
      </span>
      {f.attribution ? <span className="tag">{ATTRIBUTION_TAG[f.attribution]}</span> : <span />}
    </li>
  );
}

function integrityNote(integrity: Receipt["integrity"]): string {
  if (integrity.startsWith("Matches")) {
    return "The stored record is what it was when its hash was written. This says nothing about who wrote it.";
  }
  if (integrity.startsWith("Mismatch")) {
    return "The stored record no longer matches its hash. Something changed it after it was written. Treat every value on this receipt as unverified.";
  }
  return "This record was written before content hashing existed, so it has no integrity verdict. It is never given one after the fact.";
}
