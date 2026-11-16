"""Benchmark the scanner on a synthetic tree.

Builds a throwaway workspace of Node-style projects in a temporary directory, scans it a
few times and prints how long discovery and sizing took. The tree is removed afterwards.

    python scripts/bench.py --projects 20 --files 100000

Numbers depend heavily on the disk, the filesystem, antivirus software and whether the
operating system already has the directory entries cached, so treat them as a measurement
of this machine rather than a promise.
"""

from __future__ import annotations

import argparse
import os
import platform
import statistics
import tempfile
import time
from pathlib import Path

from sift.core.models import ScanOptions
from sift.core.scanner import scan


def build(root: Path, projects: int, files: int, per_dir: int, source_dirs: int) -> int:
    """Create ``projects`` projects holding ``files`` artifact files in total."""
    per_project = files // projects
    written = 0
    payload = b"x" * 512
    for p in range(projects):
        project = root / f"project-{p:03d}"
        (project / "node_modules").mkdir(parents=True)
        (project / "package.json").write_text("{}")
        (project / "node_modules" / ".package-lock.json").write_text("{}")
        for s in range(source_dirs):
            source = project / "src" / f"module-{s:03d}"
            source.mkdir(parents=True)
            (source / "index.js").write_bytes(payload)
        for index in range(per_project):
            directory = project / "node_modules" / f"pkg-{index // per_dir:05d}" / "lib"
            if index % per_dir == 0:
                directory.mkdir(parents=True)
            with open(directory / f"f{index % per_dir:03d}.js", "wb") as handle:
                handle.write(payload)
            written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--projects", type=int, default=20)
    parser.add_argument("--files", type=int, default=100_000, help="artifact files in total")
    parser.add_argument("--per-dir", type=int, default=20, help="files per directory")
    parser.add_argument("--source-dirs", type=int, default=50, help="source dirs per project")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument(
        "--keep",
        metavar="DIR",
        help="build the tree in DIR and leave it there (reused if it already exists)",
    )
    args = parser.parse_args()

    if args.keep:
        root = Path(args.keep)
        if not root.exists():
            root.mkdir(parents=True)
            started = time.perf_counter()
            written = build(root, args.projects, args.files, args.per_dir, args.source_dirs)
            print(f"built {written:,} artifact files in {time.perf_counter() - started:.1f}s")
        else:
            print(f"reusing {root}")
        run(root, args.runs, args.workers)
        return
    with tempfile.TemporaryDirectory(prefix="sift-bench-") as tmp:
        root = Path(tmp)
        started = time.perf_counter()
        written = build(root, args.projects, args.files, args.per_dir, args.source_dirs)
        print(f"built {written:,} artifact files in {time.perf_counter() - started:.1f}s")
        run(root, args.runs, args.workers)


def run(root: Path, runs: int, workers: int | None) -> None:
    print(f"{platform.system()} {platform.release()}, {os.cpu_count()} logical CPUs")
    discovery: list[float] = []
    sizing: list[float] = []
    total: list[float] = []
    for index in range(runs):
        result = scan(root, ScanOptions(recursive=True), workers=workers)
        discovery.append(result.stats.discovery_ms)
        sizing.append(result.stats.sizing_ms)
        total.append(result.stats.total_ms)
        print(
            f"  run {index + 1}: discovery {result.stats.discovery_ms:8.1f} ms   "
            f"sizing {result.stats.sizing_ms:8.1f} ms   total {result.stats.total_ms:8.1f} ms"
        )
    print(
        f"median: discovery {statistics.median(discovery):.1f} ms, "
        f"sizing {statistics.median(sizing):.1f} ms, total {statistics.median(total):.1f} ms "
        f"for {result.totals.artifact_count} artifacts / {result.totals.file_count:,} files "
        f"/ {result.stats.directories_visited:,} source directories"
    )
    only = scan(root, ScanOptions(recursive=True, measure=False))
    print(f"discovery only (no sizing): {only.stats.total_ms:.1f} ms")


if __name__ == "__main__":
    main()
