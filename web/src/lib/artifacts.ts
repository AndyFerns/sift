import type { Artifact, Ecosystem } from "../api/schema";

export type StatusFilter = "all" | "cleanable" | "review";
export type SortKey = "size" | "name" | "confidence" | "type";

export interface Filters {
  search: string;
  ecosystem: Ecosystem | "all";
  status: StatusFilter;
  project: string | "all";
}

export const NO_FILTERS: Filters = { search: "", ecosystem: "all", status: "all", project: "all" };

const CONFIDENCE_RANK = { high: 2, medium: 1, low: 0 } as const;

export function logicalBytes(artifact: Artifact): number {
  return artifact.size?.logical_bytes ?? 0;
}

/** The same matching rule as the terminal interface: path, type and ecosystem. */
export function filterArtifacts(artifacts: Artifact[], filters: Filters): Artifact[] {
  const needle = filters.search.trim().toLowerCase();
  return artifacts.filter((artifact) => {
    if (filters.project !== "all" && artifact.project_path !== filters.project) return false;
    if (filters.ecosystem !== "all" && artifact.ecosystem !== filters.ecosystem) return false;
    if (filters.status === "cleanable" && !artifact.cleanable) return false;
    if (filters.status === "review" && artifact.cleanable) return false;
    if (needle) {
      const haystack =
        `${artifact.relative_path} ${artifact.rule} ${artifact.ecosystem}`.toLowerCase();
      if (!haystack.includes(needle)) return false;
    }
    return true;
  });
}

export function sortArtifacts(artifacts: Artifact[], key: SortKey, reverse = false): Artifact[] {
  const sizeOf = (artifact: Artifact) => (artifact.size ? artifact.size.logical_bytes : -1);
  const byName = (a: Artifact, b: Artifact) =>
    a.relative_path.toLowerCase().localeCompare(b.relative_path.toLowerCase(), "en");
  const sorted = [...artifacts];
  if (key === "size") {
    sorted.sort((a, b) => sizeOf(b) - sizeOf(a) || byName(a, b));
  } else if (key === "name") {
    sorted.sort(byName);
  } else if (key === "confidence") {
    sorted.sort(
      (a, b) =>
        CONFIDENCE_RANK[b.confidence] - CONFIDENCE_RANK[a.confidence] ||
        Number(a.ambiguous) - Number(b.ambiguous) ||
        sizeOf(b) - sizeOf(a),
    );
  } else {
    sorted.sort((a, b) => a.rule.localeCompare(b.rule, "en") || sizeOf(b) - sizeOf(a));
  }
  return reverse ? sorted.reverse() : sorted;
}

export interface EcosystemShare {
  ecosystem: Ecosystem;
  bytes: number;
  count: number;
}

/** Size and artifact count per ecosystem, largest first. */
export function ecosystemBreakdown(artifacts: Artifact[]): EcosystemShare[] {
  const shares = new Map<Ecosystem, EcosystemShare>();
  for (const artifact of artifacts) {
    const share = shares.get(artifact.ecosystem) ?? {
      ecosystem: artifact.ecosystem,
      bytes: 0,
      count: 0,
    };
    share.bytes += logicalBytes(artifact);
    share.count += 1;
    shares.set(artifact.ecosystem, share);
  }
  return [...shares.values()].sort(
    (a, b) => b.bytes - a.bytes || a.ecosystem.localeCompare(b.ecosystem),
  );
}

export function selectedBytes(artifacts: Artifact[], selected: ReadonlySet<string>): number {
  return artifacts
    .filter((artifact) => selected.has(artifact.id))
    .reduce((total, artifact) => total + logicalBytes(artifact), 0);
}

export const ECOSYSTEM_LABELS: Record<Ecosystem, string> = {
  rust: "Rust",
  node: "Node.js",
  python: "Python",
  java: "Java",
  cpp: "C/C++",
  go: "Go",
  general: "General",
  custom: "Custom",
};
