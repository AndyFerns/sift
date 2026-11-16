"""Measuring artifacts: logical size, allocated size and file counts.

Two sizes are reported because they answer different questions:

* **logical** - the sum of file lengths; what the files contain.
* **allocated** - the disk space those files occupy. Filesystems store files in whole
  blocks, so thousands of tiny files take far more room than their contents suggest.
  This is the better guide to how much space deleting an artifact frees.

On Linux and macOS the allocated size comes straight from the filesystem (``st_blocks``)
and files with several hard links are counted once. On Windows that would cost an extra
system call per file, so Sift rounds each file up to the volume's cluster size instead and
labels the result as an estimate.

Measuring walks every directory in the artifact. Directories are handed out to a thread
pool so large trees are read in parallel. Links are counted but never followed.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from sift.core.models import SizeInfo, SizeMethod

# A worker that has more than this many directories queued hands half of them to the pool.
_SPLIT_AT = 16

Kind = Literal["directory", "file"]
OnMeasured = Callable[[str, SizeInfo | None], None]


def default_workers() -> int:
    return min(16, (os.cpu_count() or 4) * 2)


def extended_path(path: str) -> str:
    """On Windows, opt in to paths longer than 260 characters. A no-op elsewhere."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


_cluster_cache: dict[str, int | None] = {}


def cluster_size(path: str) -> int | None:
    """Allocation unit of the volume holding ``path`` (Windows only)."""
    if sys.platform != "win32":
        return None
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    if not drive:
        return None
    root = drive + "\\"
    if root not in _cluster_cache:
        import ctypes

        sectors_per_cluster = ctypes.c_ulong(0)
        bytes_per_sector = ctypes.c_ulong(0)
        free_clusters = ctypes.c_ulong(0)
        total_clusters = ctypes.c_ulong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceW(
            ctypes.c_wchar_p(root),
            ctypes.byref(sectors_per_cluster),
            ctypes.byref(bytes_per_sector),
            ctypes.byref(free_clusters),
            ctypes.byref(total_clusters),
        )
        size = sectors_per_cluster.value * bytes_per_sector.value
        _cluster_cache[root] = size if ok and size > 0 else None
    return _cluster_cache[root]


class _Job:
    """Running totals for one artifact while its directories are being walked."""

    __slots__ = (
        "aborted",
        "allocated",
        "cluster",
        "dirs",
        "done",
        "errors",
        "files",
        "links",
        "lock",
        "logical",
        "path",
        "pending",
        "seen",
    )

    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.pending = 1
        self.aborted = False
        self.logical = 0
        self.allocated = 0
        self.files = 0
        self.dirs = 0
        self.links = 0
        self.errors = 0
        self.cluster = cluster_size(path)
        self.seen: set[tuple[int, int]] = set()

    def info(self) -> SizeInfo:
        if sys.platform == "win32":
            method = SizeMethod.ESTIMATED if self.cluster else SizeMethod.UNAVAILABLE
        else:
            method = SizeMethod.EXACT
        return SizeInfo(
            logical_bytes=self.logical,
            allocated_bytes=None if method is SizeMethod.UNAVAILABLE else self.allocated,
            allocated_method=method,
            file_count=self.files,
            dir_count=self.dirs,
            link_count=self.links,
            error_count=self.errors,
            complete=self.errors == 0,
        )


class _Measurer:
    def __init__(self, workers: int, deadline: float | None, on_measured: OnMeasured | None):
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sift-size")
        self._deadline = deadline
        self._on_measured = on_measured
        self._stop = threading.Event()

    def close(self) -> None:
        self._stop.set()
        self._pool.shutdown(wait=True, cancel_futures=True)

    def start(self, path: str) -> _Job:
        job = _Job(path)
        self._pool.submit(self._walk, job, [extended_path(path)])
        return job

    def expired(self) -> bool:
        if self._stop.is_set():
            return True
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            self._stop.set()
            return True
        return False

    def _allocated(self, job: _Job, stat: os.stat_result) -> int:
        if sys.platform == "win32":
            if job.cluster:
                return -(-stat.st_size // job.cluster) * job.cluster
            return 0
        if stat.st_nlink > 1:
            key = (stat.st_dev, stat.st_ino)
            with job.lock:
                if key in job.seen:
                    return 0
                job.seen.add(key)
        return stat.st_blocks * 512

    def _walk(self, job: _Job, stack: list[str]) -> None:
        logical = allocated = files = dirs = links = errors = 0
        aborted = False
        try:
            while stack:
                if self.expired():
                    aborted = True
                    return
                current = stack.pop()
                try:
                    with os.scandir(current) as entries:
                        for entry in entries:
                            try:
                                if entry.is_symlink() or entry.is_junction():
                                    links += 1
                                elif entry.is_dir(follow_symlinks=False):
                                    dirs += 1
                                    stack.append(entry.path)
                                else:
                                    stat = entry.stat(follow_symlinks=False)
                                    files += 1
                                    logical += stat.st_size
                                    allocated += self._allocated(job, stat)
                            except OSError:
                                errors += 1
                except OSError:
                    errors += 1
                if len(stack) > _SPLIT_AT:
                    half = len(stack) // 2
                    share, stack = stack[:half], stack[half:]
                    with job.lock:
                        job.pending += 1
                    try:
                        self._pool.submit(self._walk, job, share)
                    except RuntimeError:  # pool is shutting down
                        with job.lock:
                            job.pending -= 1
                        aborted = True
                        return
        finally:
            with job.lock:
                job.logical += logical
                job.allocated += allocated
                job.files += files
                job.dirs += dirs
                job.links += links
                job.errors += errors
                job.pending -= 1
                job.aborted = job.aborted or aborted
                finished = job.pending == 0 and not job.aborted
            if finished:
                if self._on_measured is not None:
                    self._on_measured(job.path, job.info())
                job.done.set()


def measure_file(path: str) -> SizeInfo:
    """Size of a single-file artifact."""
    try:
        stat = os.lstat(extended_path(path))
    except OSError:
        return SizeInfo(error_count=1, complete=False)
    allocated: int | None
    if sys.platform == "win32":
        cluster = cluster_size(path)
        if cluster:
            allocated = -(-stat.st_size // cluster) * cluster
            method = SizeMethod.ESTIMATED
        else:
            allocated, method = None, SizeMethod.UNAVAILABLE
    else:
        allocated, method = stat.st_blocks * 512, SizeMethod.EXACT
    return SizeInfo(
        logical_bytes=stat.st_size,
        allocated_bytes=allocated,
        allocated_method=method,
        file_count=1,
    )


def measure_all(
    targets: Sequence[tuple[str, Kind]],
    *,
    workers: int | None = None,
    time_budget: float | None = None,
    on_measured: OnMeasured | None = None,
) -> dict[str, SizeInfo | None]:
    """Measure every target, in parallel.

    Returns a size for each path, or ``None`` for targets that were not finished before
    ``time_budget`` seconds elapsed. A partial walk is discarded rather than reported,
    because a number that covers only part of a directory would be misleading.

    ``on_measured`` is called from a worker thread as each directory completes.
    """
    results: dict[str, SizeInfo | None] = {}
    deadline = None if time_budget is None else time.perf_counter() + time_budget
    measurer = _Measurer(workers or default_workers(), deadline, on_measured)
    jobs: list[_Job] = []
    try:
        for path, kind in targets:
            if kind == "file":
                info = measure_file(path)
                results[path] = info
                if on_measured is not None:
                    on_measured(path, info)
            else:
                jobs.append(measurer.start(path))
        for job in jobs:
            while not job.done.is_set() and not measurer.expired():
                job.done.wait(0.02)
    finally:
        measurer.close()
    for job in jobs:
        results[job.path] = job.info() if job.done.is_set() else None
    return results


def measure(path: str, kind: Kind = "directory") -> SizeInfo:
    """Measure one artifact with no time limit."""
    info = measure_all([(path, kind)])[path]
    assert info is not None  # no time budget, so the walk always finishes
    return info
