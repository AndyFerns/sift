"""The directory walker used to measure artifacts.

This file deliberately imports nothing but the standard library, because it is used two
ways: imported by :mod:`sift.core.sizing` and run directly as a helper process
(``python _walker.py``) when a tree is large enough to be worth measuring on several CPU
cores. Helpers speak a line-oriented JSON protocol on stdin/stdout:

    request   [[directory, ...], cluster_size, budget]
    response  [logical, allocated, files, dirs, links, errors, [leftover, ...], [hardlink, ...]]

Walking is read-only. Symlinks and junctions are counted and never followed.
"""

from __future__ import annotations

import json
import os
import sys

_S_IFMT = 0o170000
_S_IFLNK = 0o120000

# (logical, allocated, files, dirs, links, errors, leftover directories, hard links)
WalkResult = tuple[int, int, int, int, int, int, list[str], list[tuple[int, int, int]]]


def walk(stack: list[str], cluster: int, budget: int) -> WalkResult:
    """Walk directories from ``stack`` until about ``budget`` entries have been seen.

    Whatever is left on the stack is returned so the caller can hand it to another worker.
    Files with more than one hard link are returned separately (device, inode, allocated)
    so the caller can count each inode once.
    """
    logical = allocated = files = dirs = links = errors = 0
    hardlinks: list[tuple[int, int, int]] = []
    scandir = os.scandir
    push = stack.append
    while stack and files + dirs < budget:
        current = stack.pop()
        try:
            with scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.is_junction():
                                links += 1
                            else:
                                dirs += 1
                                push(entry.path)
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if info.st_mode & _S_IFMT == _S_IFLNK:
                            links += 1
                            continue
                        files += 1
                        size = info.st_size
                        logical += size
                        if cluster:  # Windows: estimate from the volume's cluster size
                            allocated += (size + cluster - 1) // cluster * cluster
                        elif sys.platform != "win32":
                            if info.st_nlink > 1:
                                hardlinks.append((info.st_dev, info.st_ino, info.st_blocks * 512))
                            else:
                                allocated += info.st_blocks * 512
                    except OSError:
                        errors += 1
        except OSError:
            errors += 1
    return (logical, allocated, files, dirs, links, errors, stack, hardlinks)


def serve() -> None:
    """Helper-process loop: answer walk requests until stdin closes."""
    reader = sys.stdin.buffer
    writer = sys.stdout.buffer
    while True:
        line = reader.readline()
        if not line:
            return
        stack, cluster, budget = json.loads(line)
        writer.write(json.dumps(walk(stack, cluster, budget)).encode("ascii") + b"\n")
        writer.flush()


if __name__ == "__main__":
    serve()
