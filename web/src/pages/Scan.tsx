import { useEffect, useState, type FormEvent } from "react";

import { errorMessage } from "../api/client";
import { ErrorNote } from "../components/bits";
import { navigate } from "../router";
import { useApp } from "../state";
import { NoRoots } from "./Overview";

export function Scan() {
  const { config, scan, scanning, runScan } = useApp();
  const roots = config?.roots ?? [];
  const [path, setPath] = useState("");
  const [recursive, setRecursive] = useState(false);
  const [depth, setDepth] = useState("");
  const [error, setError] = useState<string | null>(null);

  // Start from the last scanned directory if there is one, otherwise the first root.
  useEffect(() => {
    if (path !== "") return;
    const initial = scan?.root ?? roots[0];
    if (initial) setPath(initial);
  }, [path, scan, roots]);

  if (config !== null && roots.length === 0) return <NoRoots />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await runScan({
        path: path.trim(),
        recursive,
        max_depth: depth.trim() === "" ? null : Number(depth),
      });
      navigate("explorer");
    } catch (reason) {
      setError(errorMessage(reason));
    }
  }

  return (
    <>
      <header className="page-head">
        <h1>Scan</h1>
      </header>
      <form className="card form" onSubmit={submit}>
        <fieldset disabled={scanning}>
          <legend className="sr-only">Scan options</legend>

          <label htmlFor="scan-path">Directory</label>
          <input
            id="scan-path"
            type="text"
            value={path}
            onChange={(event) => setPath(event.target.value)}
            spellCheck={false}
            autoComplete="off"
            required
          />
          <p className="hint">
            Must be one of the allowed scan roots below, or a folder inside one.
          </p>
          <div className="chips">
            {roots.map((root) => (
              <button
                key={root}
                type="button"
                className={`chip ${path === root ? "active" : ""}`}
                onClick={() => setPath(root)}
              >
                {root}
              </button>
            ))}
          </div>

          <label className="check">
            <input
              type="checkbox"
              checked={recursive}
              onChange={(event) => setRecursive(event.target.checked)}
            />
            <span>
              <strong>Recursive</strong> — scan every project under this directory, including
              nested repositories. Without this the directory is treated as one project.
            </span>
          </label>

          <label htmlFor="scan-depth">Maximum depth</label>
          <input
            id="scan-depth"
            type="number"
            min={0}
            placeholder="no limit"
            value={depth}
            onChange={(event) => setDepth(event.target.value)}
          />
          <p className="hint">How many folder levels below the directory to look. Empty for no limit.</p>

          <button className="primary" type="submit" disabled={path.trim() === ""}>
            {scanning ? "Scanning…" : "Scan"}
          </button>
          <span className="muted" role="status">
            {scanning ? " Scanning is read-only; nothing is changed on disk." : ""}
          </span>
        </fieldset>
        {error !== null && <ErrorNote message={error} />}
      </form>
    </>
  );
}
