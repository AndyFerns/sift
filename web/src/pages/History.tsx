import { useEffect, useState } from "react";

import { api, errorMessage } from "../api/client";
import type { CleanupSummary, HistoryResponse } from "../api/schema";
import { EmptyState, ErrorNote } from "../components/bits";
import { count, formatBytes, formatDuration, formatWhen } from "../lib/format";
import { navigate } from "../router";
import { useApp } from "../state";

function cleanupKind(cleanup: CleanupSummary): { label: string; className: string } {
  if (cleanup.cancelled) return { label: "cancelled", className: "result-cancelled" };
  if (cleanup.dry_run) return { label: "dry run", className: "result-dry-run" };
  if (cleanup.failed_bytes > 0) return { label: "partly failed", className: "result-partial" };
  return { label: "cleaned", className: "result-deleted" };
}

export function History() {
  const { openScan, scan } = useApp();
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Reload whenever the scan on screen changes, so new scans and cleanups show up.
  useEffect(() => {
    let stale = false;
    api
      .history()
      .then((loaded) => {
        if (!stale) setHistory(loaded);
      })
      .catch((reason: unknown) => {
        if (!stale) setError(errorMessage(reason));
      });
    return () => {
      stale = true;
    };
  }, [scan?.id]);

  async function open(id: string) {
    try {
      await openScan(id);
      navigate("explorer");
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  return (
    <>
      <header className="page-head">
        <h1>History</h1>
      </header>
      {error !== null && <ErrorNote message={error} />}
      {history === null && error === null && <p className="muted">Loading…</p>}
      {history !== null && !history.enabled && (
        <p className="warn">
          History is turned off, so only scans from this session are listed and cleanups are not
          recorded. Turn it on in Settings.
        </p>
      )}

      {history !== null && (
        <>
          <section className="card">
            <h2>Scans</h2>
            {history.scans.length === 0 ? (
              <EmptyState title="No scans yet">
                <button className="primary" onClick={() => navigate("scan")}>
                  Scan a directory
                </button>
              </EmptyState>
            ) : (
              <table className="plain" data-testid="scan-history">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Directory</th>
                    <th>Mode</th>
                    <th className="num">Artifacts</th>
                    <th className="num">Size</th>
                    <th className="num">Took</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {history.scans.map((item) => (
                    <tr key={item.id} className={scan?.id === item.id ? "current" : ""}>
                      <td>{formatWhen(item.started_at)}</td>
                      <td className="path">{item.root}</td>
                      <td>{item.recursive ? "recursive" : "single project"}</td>
                      <td className="num">{item.artifact_count}</td>
                      <td className="num">{formatBytes(item.logical_bytes)}</td>
                      <td className="num">{formatDuration(item.total_ms)}</td>
                      <td>
                        <button className="link" onClick={() => void open(item.id)}>
                          Open
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>

          <section className="card">
            <h2>Cleanups</h2>
            {history.cleanups.length === 0 ? (
              <p className="muted">No cleanups, dry runs or cancellations recorded.</p>
            ) : (
              <table className="plain" data-testid="cleanup-history">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Directory</th>
                    <th>Outcome</th>
                    <th className="num">Items</th>
                    <th className="num">Selected</th>
                    <th className="num">Deleted</th>
                    <th className="num">Failed</th>
                  </tr>
                </thead>
                <tbody>
                  {history.cleanups.map((item) => {
                    const kind = cleanupKind(item);
                    return (
                      <tr key={item.id}>
                        <td>{formatWhen(item.started_at)}</td>
                        <td className="path">{item.root}</td>
                        <td>
                          <span className={`badge ${kind.className}`}>{kind.label}</span>
                        </td>
                        <td className="num">{count(item.item_count, "artifact")}</td>
                        <td className="num">{formatBytes(item.selected_bytes)}</td>
                        <td className="num">{formatBytes(item.deleted_bytes)}</td>
                        <td className="num">
                          {item.failed_bytes > 0 ? formatBytes(item.failed_bytes) : "-"}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
          </section>
        </>
      )}
    </>
  );
}
