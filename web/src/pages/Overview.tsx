import { EcosystemName, EmptyState, SizeBar, StatusBadge } from "../components/bits";
import { ECOSYSTEM_LABELS, ecosystemBreakdown, logicalBytes } from "../lib/artifacts";
import { count, describeSize, formatBytes, formatDuration, formatWhen } from "../lib/format";
import { navigate } from "../router";
import { useApp } from "../state";

export function NoRoots() {
  const { config } = useApp();
  return (
    <EmptyState title="No scan roots configured">
      <p>
        The dashboard can only scan directories you have allowed. Add them to{" "}
        <code>{config?.config_path ?? "config.toml"}</code>:
      </p>
      <pre>{'[scan]\nroots = ["~/code"]'}</pre>
      <p>
        or restart with <code>sift serve --root PATH</code>.
      </p>
    </EmptyState>
  );
}

export function Overview() {
  const { config, scan } = useApp();

  if (config !== null && config.roots.length === 0) return <NoRoots />;
  if (scan === null) {
    return (
      <EmptyState title="Nothing scanned yet">
        <p>Run a scan to see which development artifacts are taking up space.</p>
        <button className="primary" onClick={() => navigate("scan")}>
          Scan a directory
        </button>
      </EmptyState>
    );
  }

  const { totals, stats } = scan;
  const shares = ecosystemBreakdown(scan.artifacts);
  const largest = scan.artifacts.slice(0, 8);
  const biggest = largest.length > 0 ? logicalBytes(largest[0]!) : 0;
  const biggestProject = scan.projects.reduce((max, p) => Math.max(max, p.logical_bytes), 0);
  const unmeasured = totals.artifact_count - totals.measured_count;
  const ambiguous = scan.artifacts.filter((a) => a.ambiguous).length;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Overview</h1>
          <p className="muted path">{scan.root}</p>
        </div>
        <p className="muted">
          {scan.options.recursive ? "Recursive scan" : "Single-project scan"} ·{" "}
          {formatWhen(scan.started_at)} · {formatDuration(stats.total_ms)}
        </p>
      </header>

      <section className="tiles" aria-label="Totals">
        <div className="tile">
          <span className="tile-label">Detected</span>
          <strong data-testid="total-detected">{formatBytes(totals.logical_bytes)}</strong>
          <span className="muted">
            {count(totals.artifact_count, "artifact")}, {count(totals.file_count, "file")}
          </span>
        </div>
        <div className="tile">
          <span className="tile-label">On disk</span>
          <strong>{formatBytes(totals.allocated_bytes)}</strong>
          <span className="muted">space the files occupy</span>
        </div>
        <div className="tile">
          <span className="tile-label">Cleanable</span>
          <strong className="ok" data-testid="total-cleanable">
            {formatBytes(totals.cleanable_logical_bytes)}
          </strong>
          <span className="muted">{count(totals.cleanable_count, "artifact")}</span>
        </div>
        <div className="tile">
          <span className="tile-label">Not cleanable</span>
          <strong className={totals.review_count > 0 ? "warn" : ""}>{totals.review_count}</strong>
          <span className="muted">
            {ambiguous} need review, {totals.review_count - ambiguous} low confidence
          </span>
        </div>
      </section>

      {scan.artifacts.length === 0 ? (
        <EmptyState title="No development artifacts found">
          <p>Nothing under this directory looks like a regeneratable build artifact.</p>
        </EmptyState>
      ) : (
        <>
          <section className="card">
            <h2>Size by ecosystem</h2>
            <div className="stack" role="img" aria-label="Share of detected size by ecosystem">
              {shares
                .filter((share) => share.bytes > 0)
                .map((share) => (
                  <span
                    key={share.ecosystem}
                    className={`stack-part eco-${share.ecosystem}`}
                    style={{ flexGrow: share.bytes }}
                    title={`${ECOSYSTEM_LABELS[share.ecosystem]}: ${formatBytes(share.bytes)}`}
                  />
                ))}
            </div>
            <ul className="legend">
              {shares.map((share) => (
                <li key={share.ecosystem}>
                  <EcosystemName ecosystem={share.ecosystem} />
                  <span className="num">{formatBytes(share.bytes)}</span>
                  <span className="muted">{count(share.count, "artifact")}</span>
                </li>
              ))}
            </ul>
          </section>

          <div className="columns">
            <section className="card">
              <h2>Largest artifacts</h2>
              <ul className="ranked">
                {largest.map((artifact) => (
                  <li key={artifact.id}>
                    <div className="ranked-head">
                      <span className="path">{artifact.relative_path}</span>
                      <span className="num">{describeSize(artifact.size)}</span>
                    </div>
                    <SizeBar
                      value={logicalBytes(artifact)}
                      max={biggest}
                      ecosystem={artifact.ecosystem}
                    />
                    <div className="ranked-foot muted">
                      <span>{artifact.rule}</span>
                      <StatusBadge artifact={artifact} />
                    </div>
                  </li>
                ))}
              </ul>
              <button className="link" onClick={() => navigate("explorer")}>
                See all {count(totals.artifact_count, "artifact")}
              </button>
            </section>

            <section className="card">
              <h2>Projects</h2>
              <ul className="ranked">
                {scan.projects.map((project) => (
                  <li key={project.path}>
                    <div className="ranked-head">
                      <span className="path">
                        {project.relative_path === "." ? project.name : project.relative_path}
                      </span>
                      <span className="num">{formatBytes(project.logical_bytes)}</span>
                    </div>
                    <SizeBar value={project.logical_bytes} max={biggestProject} />
                    <div className="ranked-foot muted">
                      <span>{count(project.artifact_ids.length, "artifact")}</span>
                      <span>{project.ecosystems.map((e) => ECOSYSTEM_LABELS[e]).join(", ")}</span>
                    </div>
                  </li>
                ))}
              </ul>
            </section>
          </div>
        </>
      )}

      {(scan.issues.length > 0 || unmeasured > 0) && (
        <section className="card">
          <h2>Notes from this scan</h2>
          {unmeasured > 0 && (
            <p className="warn">
              {count(unmeasured, "artifact")} not measured; the totals above leave them out.
            </p>
          )}
          <ul className="issues">
            {scan.issues.slice(0, 20).map((issue, index) => (
              <li key={`${issue.path}-${index}`}>
                <span className="badge status-review">{issue.kind}</span>{" "}
                <span className="path">{issue.path}</span> <span className="muted">{issue.message}</span>
              </li>
            ))}
          </ul>
          {scan.issues.length > 20 && (
            <p className="muted">and {scan.issues.length - 20} more.</p>
          )}
        </section>
      )}
    </>
  );
}
