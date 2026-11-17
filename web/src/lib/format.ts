import type { Artifact, SizeInfo } from "../api/schema";

const UNITS = ["KiB", "MiB", "GiB", "TiB", "PiB"] as const;

/**
 * Human-readable size in binary units, e.g. "1.5 MiB".
 *
 * This mirrors `sift.core.reports.format_bytes` exactly: both round half up to one decimal
 * using integer arithmetic, and both are tested against tests/shared/format_bytes.json,
 * so a size reads the same in the terminal and in the browser.
 */
export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "-";
  if (value < 1024) return `${value} B`;
  let index = 0;
  let unit = 1024;
  while (value >= unit * 1024 && index < UNITS.length - 1) {
    unit *= 1024;
    index += 1;
  }
  let tenths = Math.floor((value * 10 + unit / 2) / unit);
  if (tenths >= 10240 && index < UNITS.length - 1) {
    unit *= 1024;
    index += 1;
    tenths = Math.floor((value * 10 + unit / 2) / unit);
  }
  return `${Math.floor(tenths / 10)}.${tenths % 10} ${UNITS[index]}`;
}

/** "1 artifact", "2 artifacts". */
export function count(value: number, singular: string, plural?: string): string {
  const noun = value === 1 ? singular : (plural ?? `${singular}s`);
  return `${value.toLocaleString("en-US")} ${noun}`;
}

export function describeSize(size: SizeInfo | null): string {
  if (size === null) return "not measured";
  const text = formatBytes(size.logical_bytes);
  return size.complete ? text : `>= ${text}`;
}

export function describeAllocated(size: SizeInfo | null): string {
  if (size === null || size.allocated_bytes === null) return "-";
  const text = formatBytes(size.allocated_bytes);
  return size.allocated_method === "estimated" ? `~${text}` : text;
}

export function statusLabel(artifact: Artifact): string {
  if (artifact.cleanable) return "cleanable";
  return artifact.ambiguous ? "review" : "low confidence";
}

export function formatDuration(milliseconds: number): string {
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(1)} s`;
}

export function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
