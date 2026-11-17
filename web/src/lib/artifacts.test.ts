import { describe, expect, it } from "vitest";

import { fixture } from "../test/helpers";
import {
  NO_FILTERS,
  ecosystemBreakdown,
  filterArtifacts,
  logicalBytes,
  selectedBytes,
  sortArtifacts,
} from "./artifacts";

const { artifacts, totals } = fixture.scan;
const paths = (list: typeof artifacts) => list.map((a) => a.relative_path);

describe("filterArtifacts", () => {
  it("returns everything with no filters", () => {
    expect(filterArtifacts(artifacts, NO_FILTERS)).toHaveLength(artifacts.length);
  });

  it("searches path, type and ecosystem, ignoring case", () => {
    const byPath = filterArtifacts(artifacts, { ...NO_FILTERS, search: "  TARGET " });
    expect(byPath.length).toBeGreaterThan(0);
    expect(byPath.every((a) => a.relative_path.includes("target"))).toBe(true);

    const byType = filterArtifacts(artifacts, { ...NO_FILTERS, search: "python.venv" });
    expect(paths(byType).sort()).toEqual(["py-app/.venv", "py-app/venv"]);

    const byEcosystem = filterArtifacts(artifacts, { ...NO_FILTERS, search: "cpp" });
    expect(byEcosystem.length).toBe(artifacts.filter((a) => a.ecosystem === "cpp").length);
  });

  it("filters by ecosystem, status and project together", () => {
    const node = filterArtifacts(artifacts, { ...NO_FILTERS, ecosystem: "node" });
    expect(node.every((a) => a.ecosystem === "node")).toBe(true);

    const cleanable = filterArtifacts(artifacts, { ...NO_FILTERS, status: "cleanable" });
    expect(cleanable).toHaveLength(totals.cleanable_count);
    const review = filterArtifacts(artifacts, { ...NO_FILTERS, status: "review" });
    expect(review).toHaveLength(totals.review_count);
    expect(cleanable.length + review.length).toBe(artifacts.length);

    const project = fixture.scan.projects.find((p) => p.name === "py-app")!;
    const inProject = filterArtifacts(artifacts, { ...NO_FILTERS, project: project.path });
    expect(inProject.map((a) => a.id).sort()).toEqual([...project.artifact_ids].sort());

    const combined = filterArtifacts(artifacts, {
      search: "venv",
      ecosystem: "python",
      status: "review",
      project: project.path,
    });
    expect(paths(combined)).toEqual(["py-app/venv"]);
  });
});

describe("sortArtifacts", () => {
  it("sorts by size, largest first, like the scanner does", () => {
    expect(paths(sortArtifacts([...artifacts].reverse(), "size"))).toEqual(paths(artifacts));
  });

  it("sorts by path", () => {
    const sorted = paths(sortArtifacts(artifacts, "name"));
    expect(sorted).toEqual([...sorted].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase(), "en")));
    expect(paths(sortArtifacts(artifacts, "name", true))).toEqual([...sorted].reverse());
  });

  it("sorts by confidence with ambiguous artifacts after clear ones", () => {
    const rank = { high: 2, medium: 1, low: 0 };
    const sorted = sortArtifacts(artifacts, "confidence");
    const ranks = sorted.map((a) => rank[a.confidence]);
    expect(ranks).toEqual([...ranks].sort((a, b) => b - a));
    const low = sorted.filter((a) => a.confidence === "low");
    const firstAmbiguous = low.findIndex((a) => a.ambiguous);
    expect(low.slice(firstAmbiguous).every((a) => a.ambiguous)).toBe(true);
  });

  it("sorts by type", () => {
    const rules = sortArtifacts(artifacts, "type").map((a) => a.rule);
    expect(rules).toEqual([...rules].sort((a, b) => a.localeCompare(b, "en")));
  });

  it("does not modify its input", () => {
    const before = paths(artifacts);
    sortArtifacts(artifacts, "name", true);
    expect(paths(artifacts)).toEqual(before);
  });
});

describe("ecosystemBreakdown", () => {
  it("accounts for every byte and every artifact", () => {
    const shares = ecosystemBreakdown(artifacts);
    expect(shares.reduce((sum, s) => sum + s.bytes, 0)).toBe(totals.logical_bytes);
    expect(shares.reduce((sum, s) => sum + s.count, 0)).toBe(totals.artifact_count);
    const bytes = shares.map((s) => s.bytes);
    expect(bytes).toEqual([...bytes].sort((a, b) => b - a));
  });
});

describe("selectedBytes", () => {
  it("adds up only what is selected", () => {
    const [first, second] = artifacts;
    expect(selectedBytes(artifacts, new Set())).toBe(0);
    expect(selectedBytes(artifacts, new Set([first!.id, second!.id, "not-in-scan"]))).toBe(
      logicalBytes(first!) + logicalBytes(second!),
    );
    const cleanable = new Set(artifacts.filter((a) => a.cleanable).map((a) => a.id));
    expect(selectedBytes(artifacts, cleanable)).toBe(totals.cleanable_logical_bytes);
  });
});
