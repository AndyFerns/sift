import { useMemo, useState } from "react";

import { api, errorMessage } from "../api/client";
import type { Artifact, Ecosystem } from "../api/schema";
import { ArtifactDrawer } from "../components/ArtifactDrawer";
import {
  ConfidenceBadge,
  EcosystemDot,
  EmptyState,
  ErrorNote,
  SizeBar,
  StatusBadge,
} from "../components/bits";
import { CleanupDialog } from "../components/CleanupDialog";
import {
  ECOSYSTEM_LABELS,
  NO_FILTERS,
  ecosystemBreakdown,
  filterArtifacts,
  logicalBytes,
  selectedBytes,
  sortArtifacts,
  type Filters,
  type SortKey,
  type StatusFilter,
} from "../lib/artifacts";
import { count, describeAllocated, describeSize, formatBytes } from "../lib/format";
import { navigate } from "../router";
import { useApp } from "../state";

const SORTS: { key: SortKey; label: string }[] = [
  { key: "size", label: "Size" },
  { key: "name", label: "Path" },
  { key: "confidence", label: "Confidence" },
  { key: "type", label: "Type" },
];

export function Explorer() {
  const { scan, selection, toggle, select, clearSelection, rescan, scanning } = useApp();
  const [filters, setFilters] = useState<Filters>(NO_FILTERS);
  const [sortKey, setSortKey] = useState<SortKey>("size");
  const [reverse, setReverse] = useState(false);
  const [open, setOpen] = useState<Artifact | null>(null);
  const [cleaning, setCleaning] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const artifacts = useMemo(() => scan?.artifacts ?? [], [scan]);
  const visible = useMemo(
    () => sortArtifacts(filterArtifacts(artifacts, filters), sortKey, reverse),
    [artifacts, filters, sortKey, reverse],
  );
  const shares = useMemo(() => ecosystemBreakdown(artifacts), [artifacts]);
  const largest = useMemo(
    () => artifacts.reduce((max, a) => Math.max(max, logicalBytes(a)), 0),
    [artifacts],
  );

  if (scan === null) {
    return (
      <EmptyState title="Nothing to explore yet">
        <p>Scan a directory first.</p>
        <button className="primary" onClick={() => navigate("scan")}>
          Scan a directory
        </button>
      </EmptyState>
    );
  }

  const selected = artifacts.filter((a) => selection.has(a.id));
  const visibleCleanable = visible.filter((a) => a.cleanable);
  const allVisibleSelected =
    visibleCleanable.length > 0 && visibleCleanable.every((a) => selection.has(a.id));
  const filtered = filters !== NO_FILTERS && visible.length !== artifacts.length;

  function patch(change: Partial<Filters>) {
    setFilters((current) => ({ ...current, ...change }));
  }

  function toggleAllVisible() {
    if (allVisibleSelected) clearSelection();
    else select(visibleCleanable.map((a) => a.id));
  }

  async function closeCleanup(deleted: boolean) {
    setCleaning(null);
    if (!deleted) return;
    clearSelection();
    setOpen(null);
    try {
      await rescan();
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Artifacts</h1>
          <p className="muted path">{scan.root}</p>
        </div>
        <div className="head-actions">
          <a className="button" href={api.reportUrl(scan.id, "json")}>
            Export JSON
          </a>
          <a className="button" href={api.reportUrl(scan.id, "csv")}>
            Export CSV
          </a>
          <button onClick={() => void rescan().catch((r) => setError(errorMessage(r)))} disabled={scanning}>
            {scanning ? "Scanning…" : "Rescan"}
          </button>
        </div>
      </header>
      {error !== null && <ErrorNote message={error} />}

      {artifacts.length === 0 ? (
        <EmptyState title="No development artifacts found">
          <p>Nothing under this directory looks like a regeneratable build artifact.</p>
        </EmptyState>
      ) : (
        <>
          <div className="toolbar">
            <input
              type="search"
              placeholder="Search path, type or ecosystem"
              aria-label="Search artifacts"
              value={filters.search}
              onChange={(event) => patch({ search: event.target.value })}
            />
            <select
              aria-label="Status"
              value={filters.status}
              onChange={(event) => patch({ status: event.target.value as StatusFilter })}
            >
              <option value="all">Any status</option>
              <option value="cleanable">Cleanable</option>
              <option value="review">Not cleanable</option>
            </select>
            <select
              aria-label="Project"
              value={filters.project}
              onChange={(event) => patch({ project: event.target.value })}
            >
              <option value="all">All projects</option>
              {scan.projects.map((project) => (
                <option key={project.path} value={project.path}>
                  {project.relative_path === "." ? project.name : project.relative_path}
                </option>
              ))}
            </select>
            <select
              aria-label="Sort by"
              value={sortKey}
              onChange={(event) => setSortKey(event.target.value as SortKey)}
            >
              {SORTS.map((sort) => (
                <option key={sort.key} value={sort.key}>
                  Sort: {sort.label}
                </option>
              ))}
            </select>
            <button onClick={() => setReverse((value) => !value)} aria-pressed={reverse}>
              {reverse ? "Reversed" : "Reverse"}
            </button>
          </div>

          <div className="chips" role="group" aria-label="Ecosystem">
            <button
              className={`chip ${filters.ecosystem === "all" ? "active" : ""}`}
              onClick={() => patch({ ecosystem: "all" })}
            >
              All ({artifacts.length})
            </button>
            {shares.map((share) => (
              <button
                key={share.ecosystem}
                className={`chip ${filters.ecosystem === share.ecosystem ? "active" : ""}`}
                onClick={() => patch({ ecosystem: share.ecosystem as Ecosystem })}
              >
                <EcosystemDot ecosystem={share.ecosystem} />
                {ECOSYSTEM_LABELS[share.ecosystem]} ({share.count})
              </button>
            ))}
          </div>

          <p className="muted" role="status" data-testid="showing">
            Showing {visible.length} of {count(artifacts.length, "artifact")}
            {filtered && (
              <>
                {" · "}
                <button className="link" onClick={() => setFilters(NO_FILTERS)}>
                  clear filters
                </button>
              </>
            )}
          </p>

          <div className={`split ${open !== null ? "with-drawer" : ""}`}>
            <div className="table-wrap">
              {visible.length === 0 ? (
                <EmptyState title="No artifacts match these filters">
                  <button onClick={() => setFilters(NO_FILTERS)}>Clear filters</button>
                </EmptyState>
              ) : (
                <table className="artifacts">
                  <thead>
                    <tr>
                      <th>
                        <input
                          type="checkbox"
                          aria-label="Select every cleanable artifact shown"
                          checked={allVisibleSelected}
                          disabled={visibleCleanable.length === 0}
                          onChange={toggleAllVisible}
                        />
                      </th>
                      <th>Path</th>
                      <th>Type</th>
                      <th>Confidence</th>
                      <th>Status</th>
                      <th className="num">Size</th>
                      <th className="num">On disk</th>
                      <th className="num">Files</th>
                    </tr>
                  </thead>
                  <tbody>
                    {visible.map((artifact) => (
                      <tr
                        key={artifact.id}
                        data-artifact={artifact.id}
                        className={open?.id === artifact.id ? "current" : ""}
                      >
                        <td>
                          <input
                            type="checkbox"
                            aria-label={`Select ${artifact.relative_path}`}
                            checked={selection.has(artifact.id)}
                            disabled={!artifact.cleanable}
                            title={
                              artifact.cleanable
                                ? undefined
                                : `Cannot be cleaned: ${artifact.review_reason ?? "needs review"}`
                            }
                            onChange={() => toggle(artifact.id)}
                          />
                        </td>
                        <td>
                          <button className="link path" onClick={() => setOpen(artifact)}>
                            {artifact.relative_path}
                          </button>
                        </td>
                        <td>
                          <EcosystemDot ecosystem={artifact.ecosystem} />
                          {artifact.rule}
                        </td>
                        <td>
                          <ConfidenceBadge
                            confidence={artifact.confidence}
                            ambiguous={artifact.ambiguous}
                          />
                        </td>
                        <td>
                          <StatusBadge artifact={artifact} />
                        </td>
                        <td className="num size-cell">
                          <span>{describeSize(artifact.size)}</span>
                          <SizeBar
                            value={logicalBytes(artifact)}
                            max={largest}
                            ecosystem={artifact.ecosystem}
                          />
                        </td>
                        <td className="num">{describeAllocated(artifact.size)}</td>
                        <td className="num">
                          {artifact.size ? artifact.size.file_count.toLocaleString("en-US") : "-"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            {open !== null && (
              <ArtifactDrawer scanId={scan.id} artifact={open} onClose={() => setOpen(null)} />
            )}
          </div>
        </>
      )}

      {selected.length > 0 && (
        <div className="selection-bar" role="region" aria-label="Selection">
          <span data-testid="selection-summary">
            {count(selected.length, "artifact")} selected ·{" "}
            {formatBytes(selectedBytes(artifacts, selection))}
          </span>
          <button onClick={clearSelection}>Clear selection</button>
          <button className="primary" onClick={() => setCleaning(selected.map((a) => a.id))}>
            Review cleanup…
          </button>
        </div>
      )}

      {cleaning !== null && (
        <CleanupDialog
          scanId={scan.id}
          artifactIds={cleaning}
          onClose={(deleted) => void closeCleanup(deleted)}
        />
      )}
    </>
  );
}
