import type {
  ArtifactDetail,
  CleanupPlan,
  CleanupResult,
  ConfigInfo,
  Health,
  HistoryResponse,
  ScanRequest,
  ScanResult,
  ScanSummary,
  SettingsUpdate,
} from "./schema";

/** An error response from the API, or a failure to reach it at all. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...init,
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw new ApiError("Cannot reach the Sift server. Is `sift serve` still running?", 0, "offline");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    let code = "http-error";
    try {
      const body = (await response.json()) as { detail?: unknown; code?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
      if (typeof body.code === "string") code = body.code;
    } catch {
      // not JSON; keep the status line
    }
    throw new ApiError(detail, response.status, code);
  }
  return (await response.json()) as T;
}

function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: JSON.stringify(body) });
}

export const api = {
  health: () => request<Health>("/health"),
  config: () => request<ConfigInfo>("/config"),
  updateConfig: (update: SettingsUpdate) =>
    request<ConfigInfo>("/config", { method: "PUT", body: JSON.stringify(update) }),

  scan: (body: ScanRequest) => post<ScanResult>("/scans", body),
  scans: () => request<ScanSummary[]>("/scans"),
  getScan: (id: string) => request<ScanResult>(`/scans/${encodeURIComponent(id)}`),
  /** The most recent scan, or null when nothing has been scanned yet. */
  latestScan: async (): Promise<ScanResult | null> => {
    try {
      return await request<ScanResult>("/scans/latest");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  },
  artifact: (scanId: string, artifactId: string) =>
    request<ArtifactDetail>(
      `/scans/${encodeURIComponent(scanId)}/artifacts/${encodeURIComponent(artifactId)}`,
    ),
  reportUrl: (scanId: string, format: "json" | "csv") =>
    `/api/scans/${encodeURIComponent(scanId)}/report?format=${format}`,

  previewCleanup: (scanId: string, artifactIds: string[], dryRun = false) =>
    post<CleanupPlan>("/cleanup/preview", {
      scan_id: scanId,
      artifact_ids: artifactIds,
      dry_run: dryRun,
    }),
  executeCleanup: (planId: string, token = "", confirmation = "") =>
    post<CleanupResult>("/cleanup/execute", {
      plan_id: planId,
      confirmation_token: token,
      confirmation,
    }),
  cancelCleanup: (planId: string) => post<CleanupResult>("/cleanup/cancel", { plan_id: planId }),

  history: () => request<HistoryResponse>("/history"),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
