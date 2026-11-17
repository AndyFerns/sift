import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { vi } from "vitest";

import type {
  ArtifactDetail,
  CleanupPlan,
  CleanupResult,
  ConfigInfo,
  HistoryResponse,
  ScanResult,
} from "../api/schema";
import { AppProvider } from "../state";
import recorded from "./fixture.json";

/**
 * Responses recorded from the real API scanning the shared fixture workspace
 * (see scripts/record_fixture_scan.py). The tests render real scanner output.
 */
export const fixture = recorded as unknown as {
  config: ConfigInfo;
  scan: ScanResult;
  detail: ArtifactDetail;
  plan: CleanupPlan;
  dryRunResult: CleanupResult;
  history: HistoryResponse;
};

export interface Call {
  method: string;
  path: string;
  body: unknown;
}

type Handler = (body: unknown) => unknown;

/**
 * Replace `fetch` with a table of "METHOD /api/path" handlers. A handler returns the JSON
 * body, or a Response for error cases. Unknown routes fail the test loudly.
 */
export function mockApi(routes: Record<string, Handler>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const method = (init?.method ?? "GET").toUpperCase();
      const path = String(input);
      const body = typeof init?.body === "string" ? (JSON.parse(init.body) as unknown) : undefined;
      calls.push({ method, path, body });
      const handler = routes[`${method} ${path}`];
      if (handler === undefined) throw new Error(`unexpected request: ${method} ${path}`);
      const result = handler(body);
      if (result instanceof Response) return result;
      return new Response(JSON.stringify(result), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  return calls;
}

export function apiError(status: number, code: string, detail: string): Response {
  return new Response(JSON.stringify({ detail, code }), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** The two requests the app makes on start-up. */
export function startup(scan: ScanResult | null = fixture.scan, config = fixture.config) {
  return {
    "GET /api/config": () => config,
    "GET /api/scans/latest": () =>
      scan ?? apiError(404, "scan-not-found", "nothing has been scanned yet"),
  };
}

export function renderApp(ui: ReactElement) {
  return render(<AppProvider>{ui}</AppProvider>);
}
