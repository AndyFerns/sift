import { useEffect, useState, type FormEvent } from "react";

import { api, errorMessage } from "../api/client";
import type { Confidence, ConfigInfo } from "../api/schema";
import { ErrorNote } from "../components/bits";
import { useApp } from "../state";

interface Form {
  exclude: string;
  maxDepth: string;
  minConfidence: Confidence;
  timeBudget: string;
  detectors: string[];
  goDirs: string;
  history: boolean;
}

function toForm(info: ConfigInfo): Form {
  const { scan, detectors, history } = info.config;
  return {
    exclude: scan.exclude.join("\n"),
    maxDepth: scan.max_depth === null ? "" : String(scan.max_depth),
    minConfidence: scan.min_confidence,
    timeBudget: scan.time_budget_seconds === null ? "" : String(scan.time_budget_seconds),
    detectors: detectors.enabled,
    goDirs: detectors.go.artifact_dirs.join(", "),
    history: history.enabled,
  };
}

function lines(text: string, separator: RegExp): string[] {
  return text
    .split(separator)
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

export function Settings() {
  const { config, setConfig } = useApp();
  const [form, setForm] = useState<Form | null>(null);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (config !== null) setForm(toForm(config));
  }, [config]);

  if (config === null || form === null) return <p className="muted">Loading…</p>;

  function patch(change: Partial<Form>) {
    setSaved(false);
    setForm((current) => (current === null ? current : { ...current, ...change }));
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (form === null) return;
    setSaving(true);
    setError(null);
    try {
      const updated = await api.updateConfig({
        exclude: lines(form.exclude, /\r?\n/),
        max_depth: form.maxDepth.trim() === "" ? null : Number(form.maxDepth),
        min_confidence: form.minConfidence,
        time_budget_seconds: form.timeBudget.trim() === "" ? null : Number(form.timeBudget),
        detectors_enabled: form.detectors,
        go_artifact_dirs: lines(form.goDirs, /[,\s]+/),
        history_enabled: form.history,
      });
      setConfig(updated);
      setSaved(true);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <header className="page-head">
        <h1>Settings</h1>
      </header>

      <section className="card">
        <h2>Scan roots</h2>
        <p className="muted">
          The dashboard can only scan and clean inside these directories. They cannot be changed
          from the browser — edit the config file or restart with{" "}
          <code>sift serve --root PATH</code>.
        </p>
        {config.roots.length === 0 ? (
          <p className="warn">No scan roots configured.</p>
        ) : (
          <ul className="roots">
            {config.roots.map((root) => (
              <li key={root} className="path">
                {root}
              </li>
            ))}
          </ul>
        )}
        <p className="hint">
          Config file: <code>{config.config_path}</code>
          {config.config_exists ? "" : " (not created yet; showing defaults)"}
        </p>
      </section>

      <form className="card form" onSubmit={save}>
        <h2>Scanning</h2>

        <label htmlFor="exclude">Exclude patterns</label>
        <textarea
          id="exclude"
          rows={4}
          value={form.exclude}
          onChange={(event) => patch({ exclude: event.target.value })}
          spellCheck={false}
          placeholder={"vendor/\n**/fixtures/**"}
        />
        <p className="hint">
          One gitignore-style pattern per line, relative to the scanned directory. Projects can
          also have their own <code>.siftignore</code>.
        </p>

        <label htmlFor="max-depth">Maximum depth</label>
        <input
          id="max-depth"
          type="number"
          min={0}
          placeholder="no limit"
          value={form.maxDepth}
          onChange={(event) => patch({ maxDepth: event.target.value })}
        />

        <label htmlFor="min-confidence">Minimum confidence to clean</label>
        <select
          id="min-confidence"
          value={form.minConfidence}
          onChange={(event) => patch({ minConfidence: event.target.value as Confidence })}
        >
          <option value="high">High — manifest and marker both found</option>
          <option value="medium">Medium — one strong piece of evidence</option>
          <option value="low">Low — weak evidence</option>
        </select>
        <p className="hint">
          Artifacts below this are still listed but cannot be cleaned. Ambiguous artifacts can
          never be cleaned, whatever this is set to.
        </p>

        <label htmlFor="time-budget">Sizing time budget (seconds)</label>
        <input
          id="time-budget"
          type="number"
          min={0.1}
          step={0.1}
          placeholder="no limit"
          value={form.timeBudget}
          onChange={(event) => patch({ timeBudget: event.target.value })}
        />
        <p className="hint">
          When set, artifacts not measured in time are reported as "not measured" instead of
          guessed.
        </p>

        <fieldset>
          <legend>Detectors</legend>
          <div className="checks">
            {config.available_detectors.map((name) => (
              <label key={name} className="check">
                <input
                  type="checkbox"
                  checked={form.detectors.includes(name)}
                  onChange={(event) =>
                    patch({
                      detectors: event.target.checked
                        ? [...form.detectors, name]
                        : form.detectors.filter((d) => d !== name),
                    })
                  }
                />
                <span>{name}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <label htmlFor="go-dirs">Go build output directories</label>
        <input
          id="go-dirs"
          type="text"
          value={form.goDirs}
          onChange={(event) => patch({ goDirs: event.target.value })}
          placeholder="bin, out"
          spellCheck={false}
        />
        <p className="hint">
          Go has no standard build directory. Names listed here are treated as artifacts when
          they sit next to a <code>go.mod</code>.
        </p>

        <label className="check">
          <input
            type="checkbox"
            checked={form.history}
            onChange={(event) => patch({ history: event.target.checked })}
          />
          <span>
            <strong>Keep history</strong> — record scans and cleanup audits in a local database.
          </span>
        </label>

        <div className="form-actions">
          <button className="primary" type="submit" disabled={saving}>
            {saving ? "Saving…" : "Save settings"}
          </button>
          <span className="muted" role="status">
            {saved ? "Saved. New scans will use these settings." : ""}
          </span>
        </div>
        <p className="hint">Saving rewrites the config file; comments in it are not kept.</p>
        {error !== null && <ErrorNote message={error} />}
      </form>
    </>
  );
}
