import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { api, errorMessage } from "../api/client";
import type { CleanupPlan, CleanupResult } from "../api/schema";
import { count, formatBytes } from "../lib/format";
import { ErrorNote } from "./bits";

export const CONFIRMATION_WORD = "delete";

type Phase = "loading" | "preview" | "working" | "result" | "failed";

interface Props {
  scanId: string;
  artifactIds: string[];
  /** Called when the dialog closes. `deleted` is true only after a real cleanup ran. */
  onClose: (deleted: boolean) => void;
}

/**
 * The whole cleanup flow in one dialog: preview, typed confirmation, result.
 *
 * Nothing is deleted by opening it. Cancel has focus, Escape cancels, and the delete
 * button stays disabled until the confirmation word has been typed.
 */
export function CleanupDialog({ scanId, artifactIds, onClose }: Props) {
  const [phase, setPhase] = useState<Phase>("loading");
  const [plan, setPlan] = useState<CleanupPlan | null>(null);
  const [result, setResult] = useState<CleanupResult | null>(null);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    let stale = false;
    api
      .previewCleanup(scanId, artifactIds)
      .then((preview) => {
        if (stale) return;
        setPlan(preview);
        setPhase("preview");
      })
      .catch((reason: unknown) => {
        if (stale) return;
        setError(errorMessage(reason));
        setPhase("failed");
      });
    return () => {
      stale = true;
    };
  }, [scanId, artifactIds]);

  useEffect(() => {
    if (phase === "preview") cancelButton.current?.focus();
    if (phase === "result" || phase === "failed") closeButton.current?.focus();
  }, [phase]);

  const confirmed = typed.trim().toLowerCase() === CONFIRMATION_WORD;
  const deletable = plan?.items.filter((item) => item.allowed) ?? [];
  const refused = plan?.items.filter((item) => !item.allowed) ?? [];

  async function cancel() {
    if (phase === "working") return;
    if (phase === "preview" && plan !== null) {
      // Record that the user backed out. Closing must not depend on this succeeding.
      await api.cancelCleanup(plan.id).catch(() => undefined);
    }
    onClose(false);
  }

  async function dryRun() {
    if (plan === null) return;
    setPhase("working");
    try {
      await api.cancelCleanup(plan.id).catch(() => undefined);
      const rehearsal = await api.previewCleanup(scanId, artifactIds, true);
      setResult(await api.executeCleanup(rehearsal.id));
      setPhase("result");
    } catch (reason) {
      setError(errorMessage(reason));
      setPhase("failed");
    }
  }

  async function execute() {
    if (plan === null || !confirmed) return;
    setPhase("working");
    try {
      setResult(await api.executeCleanup(plan.id, plan.confirmation_token, typed));
      setPhase("result");
    } catch (reason) {
      setError(errorMessage(reason));
      setPhase("failed");
    }
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.key !== "Escape") return;
    event.stopPropagation();
    if (phase === "result" || phase === "failed") onClose(result !== null && !result.dry_run);
    else void cancel();
  }

  return (
    <div className="overlay" onKeyDown={onKeyDown}>
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="cleanup-title">
        {phase === "loading" && (
          <>
            <h2 id="cleanup-title">Building the cleanup preview…</h2>
            <p className="muted">Checking each selected artifact. Nothing is being deleted.</p>
          </>
        )}

        {phase === "failed" && (
          <>
            <h2 id="cleanup-title">Cleanup did not run</h2>
            <ErrorNote message={error ?? "Something went wrong."} />
            <div className="dialog-actions">
              <button ref={closeButton} className="primary" onClick={() => onClose(false)}>
                Close
              </button>
            </div>
          </>
        )}

        {(phase === "preview" || phase === "working") && plan !== null && (
          <>
            <h2 id="cleanup-title">Cleanup preview</h2>
            <p className="muted">Nothing has been deleted. Review exactly what will be removed.</p>

            {deletable.length > 0 ? (
              <>
                <h3>Will be deleted ({deletable.length})</h3>
                <table className="plain" data-testid="will-delete">
                  <thead>
                    <tr>
                      <th>Path</th>
                      <th>Type</th>
                      <th>Confidence</th>
                      <th className="num">Files</th>
                      <th className="num">Size</th>
                    </tr>
                  </thead>
                  <tbody>
                    {deletable.map((item) => (
                      <tr key={item.artifact_id}>
                        <td className="path">{item.path}</td>
                        <td>{item.rule}</td>
                        <td>{item.confidence}</td>
                        <td className="num">{item.file_count.toLocaleString("en-US")}</td>
                        <td className="num">{formatBytes(item.logical_bytes)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : (
              <p className="warn">Nothing in the selection can be deleted.</p>
            )}

            {refused.length > 0 && (
              <>
                <h3 className="warn">Refused — will NOT be deleted ({refused.length})</h3>
                <table className="plain" data-testid="refused">
                  <tbody>
                    {refused.map((item) => (
                      <tr key={item.artifact_id}>
                        <td className="path">{item.path}</td>
                        <td>{item.refusal_reason ?? "refused"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}

            <dl className="totals">
              <div>
                <dt>Detected in scan</dt>
                <dd>{formatBytes(plan.totals.detected_bytes)}</dd>
              </div>
              <div>
                <dt>Selected</dt>
                <dd>{formatBytes(plan.totals.selected_bytes)}</dd>
              </div>
              <div>
                <dt>To delete</dt>
                <dd data-testid="to-delete">
                  {formatBytes(plan.totals.deletable_bytes)} ·{" "}
                  {count(plan.totals.deletable_count, "artifact")} ·{" "}
                  {count(plan.totals.deletable_file_count, "file")}
                </dd>
              </div>
            </dl>

            {deletable.length > 0 && (
              <div className="confirm">
                <p className="danger">
                  <strong>Deletion is permanent.</strong> Sift cannot restore deleted files.
                </p>
                <label htmlFor="confirm-input">
                  Type <code>{CONFIRMATION_WORD}</code> to confirm. Anything else, Cancel or
                  Escape backs out.
                </label>
                <input
                  id="confirm-input"
                  type="text"
                  value={typed}
                  onChange={(event) => setTyped(event.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={phase === "working"}
                />
              </div>
            )}

            <div className="dialog-actions">
              <button
                ref={cancelButton}
                className="primary"
                onClick={() => void cancel()}
                disabled={phase === "working"}
              >
                Cancel — delete nothing
              </button>
              <button onClick={() => void dryRun()} disabled={phase === "working"}>
                Dry run
              </button>
              <button
                className="danger-button"
                onClick={() => void execute()}
                disabled={!confirmed || deletable.length === 0 || phase === "working"}
              >
                {phase === "working" ? "Working…" : "Delete permanently"}
              </button>
            </div>
          </>
        )}

        {phase === "result" && result !== null && (
          <>
            <h2 id="cleanup-title">
              {result.dry_run ? "Dry run — nothing was deleted" : "Cleanup finished"}
            </h2>
            <table className="plain" data-testid="result">
              <thead>
                <tr>
                  <th>Path</th>
                  <th>Result</th>
                  <th className="num">Deleted</th>
                  <th className="num">Failed</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((item) => (
                  <tr key={item.artifact_id}>
                    <td className="path">
                      {item.path}
                      {item.message && <div className="muted">{item.message}</div>}
                      {item.errors.slice(0, 3).map((line) => (
                        <div key={line} className="muted">
                          {line}
                        </div>
                      ))}
                    </td>
                    <td>
                      <span className={`badge result-${item.status}`}>{item.status}</span>
                    </td>
                    <td className="num">{formatBytes(item.deleted_bytes)}</td>
                    <td className="num">
                      {item.failed_bytes > 0 ? formatBytes(item.failed_bytes) : "-"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <dl className="totals">
              <div>
                <dt>Detected</dt>
                <dd>{formatBytes(result.totals.detected_bytes)}</dd>
              </div>
              <div>
                <dt>Selected</dt>
                <dd>{formatBytes(result.totals.selected_bytes)}</dd>
              </div>
              {result.dry_run && (
                <div>
                  <dt>Would delete</dt>
                  <dd>{formatBytes(result.totals.would_delete_bytes)}</dd>
                </div>
              )}
              <div>
                <dt>Deleted</dt>
                <dd data-testid="deleted">{formatBytes(result.totals.deleted_bytes)}</dd>
              </div>
              <div>
                <dt>Failed</dt>
                <dd>{formatBytes(result.totals.failed_bytes)}</dd>
              </div>
              <div>
                <dt>Refused</dt>
                <dd>{formatBytes(result.totals.refused_bytes)}</dd>
              </div>
            </dl>
            <div className="dialog-actions">
              <button
                ref={closeButton}
                className="primary"
                onClick={() => onClose(!result.dry_run)}
              >
                Close
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
