import { useEffect, useState } from "react";

import { api, errorMessage } from "../api/client";
import type { Artifact, ArtifactDetail } from "../api/schema";
import { count, describeAllocated, describeSize, formatBytes } from "../lib/format";
import { ConfidenceBadge, EcosystemName, ErrorNote, SizeBar, StatusBadge } from "./bits";

interface Props {
  scanId: string;
  artifact: Artifact;
  onClose: () => void;
}

/** Everything known about one artifact, re-checked on disk when the panel opens. */
export function ArtifactDrawer({ scanId, artifact, onClose }: Props) {
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setDetail(null);
    setError(null);
    api
      .artifact(scanId, artifact.id)
      .then((loaded) => {
        if (!stale) setDetail(loaded);
      })
      .catch((reason: unknown) => {
        if (!stale) setError(errorMessage(reason));
      });
    return () => {
      stale = true;
    };
  }, [scanId, artifact.id]);

  const size = detail?.current_size ?? artifact.size;
  const largestChild = detail?.children.reduce((max, c) => Math.max(max, c.logical_bytes), 0) ?? 0;

  return (
    <aside className="drawer" aria-label={`Details for ${artifact.relative_path}`}>
      <header>
        <h2>{artifact.name}</h2>
        <button onClick={onClose} aria-label="Close details">
          Close
        </button>
      </header>
      <p className="path">{artifact.path}</p>
      <p>{artifact.description}</p>

      <dl className="facts">
        <dt>ID</dt>
        <dd>
          <code>{artifact.id}</code>
        </dd>
        <dt>Type</dt>
        <dd>
          {artifact.rule} · <EcosystemName ecosystem={artifact.ecosystem} />
        </dd>
        <dt>Confidence</dt>
        <dd>
          <ConfidenceBadge confidence={artifact.confidence} ambiguous={artifact.ambiguous} />{" "}
          <StatusBadge artifact={artifact} />
        </dd>
        {artifact.review_reason && (
          <>
            <dt>Why not cleanable</dt>
            <dd className="warn">{artifact.review_reason}</dd>
          </>
        )}
        <dt>Size in files</dt>
        <dd>{describeSize(size)}</dd>
        <dt>Size on disk</dt>
        <dd>
          {describeAllocated(size)}
          {size?.allocated_method === "estimated" && (
            <span className="muted"> (estimated from the volume's cluster size)</span>
          )}
        </dd>
        {size && (
          <>
            <dt>Contents</dt>
            <dd>
              {count(size.file_count, "file")}, {count(size.dir_count, "directory", "directories")}
              {size.link_count > 0 ? `, ${count(size.link_count, "link")} (not followed)` : ""}
            </dd>
          </>
        )}
        <dt>Project</dt>
        <dd className="path">{artifact.project_path}</dd>
      </dl>
      <p className="hint">
        "In files" is what the files contain. "On disk" is the space they occupy, which is what
        deleting them frees.
      </p>

      <h3>Evidence</h3>
      <ul className="evidence">
        {artifact.evidence.map((item, index) => (
          <li key={`${item.kind}-${index}`}>
            <span className={`badge evidence-${item.kind}`}>{item.kind}</span> {item.detail}
          </li>
        ))}
      </ul>

      {error !== null && <ErrorNote message={error} />}
      {detail === null && error === null && <p className="muted">Checking on disk…</p>}

      {detail !== null && (
        <>
          <h3>On disk right now</h3>
          {!detail.exists ? (
            <p className="warn">This artifact no longer exists.</p>
          ) : !detail.still_matches ? (
            <p className="warn">It no longer matches the type it had when it was scanned.</p>
          ) : (
            <p className="muted">Still there and still matches.</p>
          )}
          <p className={detail.cleanup_allowed ? "ok" : "warn"} data-testid="cleanup-verdict">
            {detail.cleanup_allowed
              ? "Cleanup allowed, after preview and confirmation."
              : `Cleanup refused: ${detail.cleanup_refusal ?? "not allowed"}`}
          </p>

          {detail.children.length > 0 && (
            <>
              <h3>Largest entries inside</h3>
              <ul className="ranked compact">
                {detail.children.map((child) => (
                  <li key={child.name}>
                    <div className="ranked-head">
                      <span className="path">
                        {child.name}
                        {child.kind === "directory" ? "/" : ""}
                      </span>
                      <span className="num">{formatBytes(child.logical_bytes)}</span>
                    </div>
                    <SizeBar
                      value={child.logical_bytes}
                      max={largestChild}
                      ecosystem={artifact.ecosystem}
                    />
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </aside>
  );
}
