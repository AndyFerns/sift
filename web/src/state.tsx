import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { api, errorMessage } from "./api/client";
import type { ConfigInfo, ScanRequest, ScanResult } from "./api/schema";

interface AppState {
  /** null until the first load finishes. */
  config: ConfigInfo | null;
  /** The scan currently on screen. null means nothing has been scanned yet. */
  scan: ScanResult | null;
  selection: ReadonlySet<string>;
  loading: boolean;
  scanning: boolean;
  error: string | null;
  runScan: (request: ScanRequest) => Promise<ScanResult>;
  /** Scan the current scan's root again with the same options. */
  rescan: () => Promise<void>;
  openScan: (id: string) => Promise<void>;
  setConfig: (config: ConfigInfo) => void;
  toggle: (id: string) => void;
  select: (ids: string[]) => void;
  clearSelection: () => void;
}

const Context = createContext<AppState | null>(null);

export function AppProvider({ children }: { children: ReactNode }) {
  const [config, setConfig] = useState<ConfigInfo | null>(null);
  const [scan, setScan] = useState<ScanResult | null>(null);
  const [selection, setSelection] = useState<ReadonlySet<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.config(), api.latestScan()])
      .then(([loadedConfig, latest]) => {
        if (cancelled) return;
        setConfig(loadedConfig);
        setScan(latest);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(errorMessage(reason));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const show = useCallback((result: ScanResult) => {
    setScan(result);
    // Only artifacts that are in the scan on screen, and cleanable, can stay selected.
    const cleanable = new Set(result.artifacts.filter((a) => a.cleanable).map((a) => a.id));
    setSelection((current) => new Set([...current].filter((id) => cleanable.has(id))));
  }, []);

  const runScan = useCallback(
    async (request: ScanRequest) => {
      setScanning(true);
      try {
        const result = await api.scan(request);
        show(result);
        return result;
      } finally {
        setScanning(false);
      }
    },
    [show],
  );

  const rescan = useCallback(async () => {
    if (scan === null) return;
    await runScan({
      path: scan.root,
      recursive: scan.options.recursive,
      max_depth: scan.options.max_depth,
    });
  }, [scan, runScan]);

  const openScan = useCallback(
    async (id: string) => {
      show(await api.getScan(id));
    },
    [show],
  );

  const toggle = useCallback((id: string) => {
    setSelection((current) => {
      const next = new Set(current);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  }, []);

  const select = useCallback((ids: string[]) => {
    setSelection((current) => new Set([...current, ...ids]));
  }, []);

  const clearSelection = useCallback(() => setSelection(new Set()), []);

  const value = useMemo<AppState>(
    () => ({
      config,
      scan,
      selection,
      loading,
      scanning,
      error,
      runScan,
      rescan,
      openScan,
      setConfig,
      toggle,
      select,
      clearSelection,
    }),
    [
      config,
      scan,
      selection,
      loading,
      scanning,
      error,
      runScan,
      rescan,
      openScan,
      toggle,
      select,
      clearSelection,
    ],
  );

  return <Context.Provider value={value}>{children}</Context.Provider>;
}

export function useApp(): AppState {
  const state = useContext(Context);
  if (state === null) throw new Error("useApp must be used inside <AppProvider>");
  return state;
}
