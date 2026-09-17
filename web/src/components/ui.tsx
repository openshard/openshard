import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { CaptureDepth, Integrity, TaskStatus, VerificationStatus } from "../api/types";
import { statusLabel } from "../lib/format";

type Tone = "ok" | "bad" | "warn" | "neutral" | "accent";

export function Pill({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`pill ${tone}`} title={title}>
      {children}
    </span>
  );
}

const STATUS_TONE: Record<TaskStatus, Tone> = { completed: "ok", failed: "bad", in_progress: "warn" };

export function StatusPill({ status }: { status: TaskStatus }) {
  return <Pill tone={STATUS_TONE[status]}>{statusLabel(status)}</Pill>;
}

/**
 * Integrity wording is the CLI's, verbatim. "Matches" is tamper-evidence
 * for the stored record only and never implies who wrote it.
 */
export function IntegrityPill({ integrity, short = false }: { integrity: Integrity; short?: boolean }) {
  const tone: Tone = integrity.startsWith("Matches") ? "ok" : integrity.startsWith("Mismatch") ? "bad" : "neutral";
  const text = short ? integrity.replace(" (content hash)", "") : integrity;
  return (
    <Pill tone={tone} title={`Integrity: ${integrity}`}>
      {text}
    </Pill>
  );
}

export function CapturePill({ depth }: { depth: CaptureDepth }) {
  if (depth === "full") return null;
  return <Pill tone={depth === "partial" ? "warn" : "neutral"}>{depth} capture</Pill>;
}

const VERIFICATION_TONE: Record<VerificationStatus, Tone> = {
  passed: "ok",
  failed: "bad",
  not_run: "neutral",
  skipped: "neutral",
  manual_review: "warn",
  unknown: "neutral",
};

export function ChecksText({ verification, checks }: { verification: VerificationStatus; checks: string }) {
  const tone = VERIFICATION_TONE[verification];
  const color = tone === "ok" ? "var(--ok)" : tone === "bad" ? "var(--bad)" : "var(--muted)";
  return <span style={{ color }}>{checks}</span>;
}

export function Section({
  title,
  aside,
  children,
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="section">
      <div className="section-head">
        <h2>{title}</h2>
        {aside ? <span className="aside">{aside}</span> : null}
      </div>
      <div className="section-body">{children}</div>
    </section>
  );
}

/** Label / value rows, the receipt's native shape. Missing values are said out loud by callers. */
export function Rows({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="rows">
      {items.map(([label, value]) => (
        <RowPair key={label} label={label} value={value} />
      ))}
    </dl>
  );
}

function RowPair({ label, value }: { label: string; value: ReactNode }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </>
  );
}

export function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="fact">
      <div className="label">{label}</div>
      <div className="value">{children}</div>
    </div>
  );
}

export function Crumbs({ items }: { items: { to?: string; label: string }[] }) {
  return (
    <div className="crumbs">
      {items.map((item, i) => (
        <span key={i}>
          {i > 0 ? <span className="sep">/ </span> : null}
          {item.to ? <Link to={item.to}>{item.label}</Link> : <span>{item.label}</span>}
        </span>
      ))}
    </div>
  );
}

export function Muted({ children }: { children: ReactNode }) {
  return <span className="muted">{children}</span>;
}

export function LoadingState({ what }: { what: string }) {
  return <div className="state">Loading {what}…</div>;
}

export function ErrorState({ message }: { message: string }) {
  return <div className="state error">Could not load: {message}</div>;
}

export function NotFound({ what, id }: { what: string; id: string }) {
  return (
    <div className="state">
      No {what} with id <code>{id}</code>. <Link to="/">Back to recent work</Link>.
    </div>
  );
}
