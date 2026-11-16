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

How it stays fast
-----------------
Measuring means visiting every directory in the artifact; there is no shortcut. Work is
shared out as chunks of directories:

* Two threads start immediately. Small scans finish here, in milliseconds, without
  paying for anything else.
* If measuring is still going after a moment, helper *processes* are started and take
  chunks too. Walking is CPU-bound in Python once the directory entries are cached, so
  threads alone cannot use more than one core; helper processes can.

Links are counted but never followed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from typing import Literal

from sift.core import _walker
from sift.core.models import SizeInfo, SizeMethod

Kind = Literal["directory", "file"]
OnMeasured = Callable[[str, SizeInfo | None], None]
WalkFunction = Callable[[list[str], int, int], _walker.WalkResult]

_LOCAL_THREADS = 2
_CHUNK_DIRS = 24  # directories handed over in one chunk
_BUDGET = 3000  # entries a worker examines before sharing out what it has left
_HELPER_DELAY = 0.25  # seconds of measuring before helper processes are worth starting
_MAX_HELPERS = 8


def default_helpers() -> int:
    """How many helper processes to use for large trees."""
    return max(1, min(_MAX_HELPERS, (os.cpu_count() or 2) - 2))


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
        "logical",
        "path",
        "pending",
        "seen",
    )

    def __init__(self, path: str) -> None:
        self.path = path
        self.done = threading.Event()
        self.pending = 1  # chunks queued or being walked
        self.aborted = False
        self.logical = 0
        self.allocated = 0
        self.files = 0
        self.dirs = 0
        self.links = 0
        self.errors = 0
        self.cluster = cluster_size(path) or 0
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


class _Helper:
    """One helper process running :mod:`sift.core._walker`."""

    def __init__(self) -> None:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        # -I and -S: the helper needs only the standard library, so skip site-packages
        # and anything the environment could inject. It starts faster that way too.
        self._process = subprocess.Popen(
            [sys.executable, "-I", "-S", _walker.__file__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )

    def walk(self, stack: list[str], cluster: int, budget: int) -> _walker.WalkResult:
        process = self._process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(json.dumps([stack, cluster, budget]).encode("ascii") + b"\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("the helper process exited")
        logical, allocated, files, dirs, links, errors, leftover, hardlinks = json.loads(line)
        return (
            logical,
            allocated,
            files,
            dirs,
            links,
            errors,
            leftover,
            [(dev, ino, size) for dev, ino, size in hardlinks],
        )

    def hang_up(self) -> None:
        """Close the request pipe; the helper exits once it has answered what it was asked."""
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except OSError:
            pass

    def reap(self) -> None:
        process = self._process
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None:
            process.stdout.close()


class _Measurer:
    def __init__(self, deadline: float | None, on_measured: OnMeasured | None) -> None:
        self._deadline = deadline
        self._on_measured = on_measured
        self._ready = threading.Condition()
        self._queue: deque[tuple[_Job, list[str]]] = deque()
        self._closing = False
        self._expired = False
        self._threads: list[threading.Thread] = []
        self._helpers: list[_Helper] = []
        for _ in range(_LOCAL_THREADS):
            self._spawn(_walker.walk)

    def _spawn(self, function: WalkFunction) -> None:
        thread = threading.Thread(target=self._work, args=(function,), daemon=True)
        thread.name = "sift-size"
        self._threads.append(thread)
        thread.start()

    def start_helpers(self, count: int) -> None:
        """Bring up helper processes. Failure to start one is not an error."""
        if not sys.executable or getattr(sys, "frozen", False):
            return
        for _ in range(count):
            try:
                helper = _Helper()
            except OSError:
                return
            self._helpers.append(helper)
            self._spawn(helper.walk)

    def close(self) -> None:
        with self._ready:
            self._closing = True
            self._ready.notify_all()
        for helper in self._helpers:
            helper.hang_up()
        for thread in self._threads:
            thread.join()
        for helper in self._helpers:
            helper.reap()

    def expired(self) -> bool:
        if self._expired:
            return True
        if self._deadline is not None and time.perf_counter() >= self._deadline:
            self._expired = True
        return self._expired

    def submit(self, path: str) -> _Job:
        job = _Job(path)
        with self._ready:
            self._queue.append((job, [extended_path(path)]))
            self._ready.notify()
        return job

    def _work(self, function: WalkFunction) -> None:
        while True:
            with self._ready:
                while not self._queue and not self._closing:
                    self._ready.wait()
                if self._closing:
                    return
                job, stack = self._queue.popleft()
            if self.expired():
                self._settle(job, None)
                continue
            try:
                result = function(stack, job.cluster, _BUDGET)
            except Exception:
                if function is _walker.walk or self._closing:
                    self._settle(job, None)
                    continue
                # A helper process died. Do this chunk here and retire the thread.
                self._settle(job, _walker.walk(stack, job.cluster, _BUDGET))
                return
            self._settle(job, result)

    def _settle(self, job: _Job, result: _walker.WalkResult | None) -> None:
        """Fold one chunk's totals into its job and queue whatever it left unwalked."""
        with self._ready:
            job.pending -= 1
            if result is None:
                job.aborted = True
            else:
                logical, allocated, files, dirs, links, errors, leftover, hardlinks = result
                job.logical += logical
                job.allocated += allocated
                job.files += files
                job.dirs += dirs
                job.links += links
                job.errors += errors
                for device, inode, size in hardlinks:
                    if (device, inode) not in job.seen:
                        job.seen.add((device, inode))
                        job.allocated += size
                for start in range(0, len(leftover), _CHUNK_DIRS):
                    self._queue.append((job, leftover[start : start + _CHUNK_DIRS]))
                    job.pending += 1
                if leftover:
                    self._ready.notify_all()
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
    helper_delay: float = _HELPER_DELAY,
) -> dict[str, SizeInfo | None]:
    """Measure every target, in parallel.

    Returns a size for each path, or ``None`` for targets that were not finished before
    ``time_budget`` seconds elapsed. A partial walk is discarded rather than reported,
    because a number that covers only part of a directory would be misleading.

    ``workers`` is the number of helper processes allowed for large trees (default: based
    on the CPU count; 0 disables them). ``on_measured`` is called from a worker thread as
    each directory completes.
    """
    results: dict[str, SizeInfo | None] = {}
    started = time.perf_counter()
    deadline = None if time_budget is None else started + time_budget
    helpers = default_helpers() if workers is None else workers
    measurer = _Measurer(deadline, on_measured)
    jobs: list[_Job] = []
    try:
        for path, kind in targets:
            if kind == "file":
                info = measure_file(path)
                results[path] = info
                if on_measured is not None:
                    on_measured(path, info)
            else:
                jobs.append(measurer.submit(path))
        for job in jobs:
            while not job.done.is_set() and not measurer.expired():
                job.done.wait(0.02)
                if helpers and time.perf_counter() - started >= helper_delay:
                    measurer.start_helpers(helpers)
                    helpers = 0
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
