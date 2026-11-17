import type { ReactNode } from "react";

import type { Artifact, Confidence, Ecosystem } from "../api/schema";
import { ECOSYSTEM_LABELS } from "../lib/artifacts";
import { statusLabel } from "../lib/format";

export function ConfidenceBadge({
  confidence,
  ambiguous,
}: {
  confidence: Confidence;
  ambiguous: boolean;
}) {
  return (
    <span className={`badge confidence-${confidence}`}>
      {confidence}
      {ambiguous ? " ?" : ""}
    </span>
  );
}

export function StatusBadge({ artifact }: { artifact: Artifact }) {
  const label = statusLabel(artifact);
  return (
    <span
      className={`badge ${artifact.cleanable ? "status-ok" : "status-review"}`}
      title={artifact.review_reason ?? undefined}
    >
      {label}
    </span>
  );
}

export function EcosystemDot({ ecosystem }: { ecosystem: Ecosystem }) {
  return <span className={`dot eco-${ecosystem}`} aria-hidden="true" />;
}

export function EcosystemName({ ecosystem }: { ecosystem: Ecosystem }) {
  return (
    <span className="eco-name">
      <EcosystemDot ecosystem={ecosystem} />
      {ECOSYSTEM_LABELS[ecosystem]}
    </span>
  );
}

/** A horizontal bar whose length is `value` as a share of `max`. */
export function SizeBar({
  value,
  max,
  ecosystem,
}: {
  value: number;
  max: number;
  ecosystem?: Ecosystem;
}) {
  const percent = max > 0 ? Math.max((value / max) * 100, value > 0 ? 1 : 0) : 0;
  return (
    <span className="bar" aria-hidden="true">
      <span
        className={`bar-fill ${ecosystem ? `eco-${ecosystem}` : ""}`}
        style={{ width: `${percent}%` }}
      />
    </span>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <h2>{title}</h2>
      {children}
    </div>
  );
}

export function ErrorNote({ message }: { message: string }) {
  return (
    <p className="error" role="alert">
      {message}
    </p>
  );
}
