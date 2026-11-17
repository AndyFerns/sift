import { describe, expect, it } from "vitest";

import sharedCases from "../../../tests/shared/format_bytes.json";
import { fixture } from "../test/helpers";
import {
  count,
  describeAllocated,
  describeSize,
  formatBytes,
  formatDuration,
  statusLabel,
} from "./format";

describe("formatBytes", () => {
  // The Python test suite checks sift.core.reports.format_bytes against this same file.
  it.each(sharedCases as [number, string][])("formats %d as %s", (value, text) => {
    expect(formatBytes(value)).toBe(text);
  });

  it("shows a dash when there is no value", () => {
    expect(formatBytes(null)).toBe("-");
    expect(formatBytes(undefined)).toBe("-");
  });
});

describe("count", () => {
  it("pluralises", () => {
    expect(count(0, "artifact")).toBe("0 artifacts");
    expect(count(1, "artifact")).toBe("1 artifact");
    expect(count(2, "artifact")).toBe("2 artifacts");
    expect(count(1, "directory", "directories")).toBe("1 directory");
    expect(count(3, "directory", "directories")).toBe("3 directories");
    expect(count(12345, "file")).toBe("12,345 files");
  });
});

describe("size descriptions", () => {
  const size = fixture.scan.artifacts[0]!.size!;

  it("describes measured sizes", () => {
    expect(describeSize(size)).toBe(formatBytes(size.logical_bytes));
    expect(describeSize({ ...size, complete: false })).toBe(
      `>= ${formatBytes(size.logical_bytes)}`,
    );
    expect(describeSize(null)).toBe("not measured");
  });

  it("marks estimated allocated sizes", () => {
    expect(describeAllocated({ ...size, allocated_bytes: 4096, allocated_method: "exact" })).toBe(
      "4.0 KiB",
    );
    expect(
      describeAllocated({ ...size, allocated_bytes: 4096, allocated_method: "estimated" }),
    ).toBe("~4.0 KiB");
    expect(
      describeAllocated({ ...size, allocated_bytes: null, allocated_method: "unavailable" }),
    ).toBe("-");
    expect(describeAllocated(null)).toBe("-");
  });
});

describe("statusLabel", () => {
  it("labels every artifact in the recorded scan", () => {
    for (const artifact of fixture.scan.artifacts) {
      const expected = artifact.cleanable
        ? "cleanable"
        : artifact.ambiguous
          ? "review"
          : "low confidence";
      expect(statusLabel(artifact)).toBe(expected);
    }
  });
});

describe("formatDuration", () => {
  it("uses milliseconds below a second", () => {
    expect(formatDuration(46.4)).toBe("46 ms");
    expect(formatDuration(1893)).toBe("1.9 s");
  });
});
