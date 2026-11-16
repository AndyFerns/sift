"""Exclusions: config ``exclude`` patterns and per-directory ``.siftignore`` files.

Patterns use gitignore syntax. Each ``.siftignore`` applies to the directory it sits in and
everything below it. Layers are evaluated independently: a path is excluded as soon as any
layer excludes it, so a ``!negation`` only undoes patterns from its own file.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from pathspec import GitIgnoreSpec

IGNORE_FILE = ".siftignore"
_MAX_IGNORE_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class IgnoreLayer:
    base: str
    spec: GitIgnoreSpec

    def matches(self, path: str, is_dir: bool) -> bool:
        relative = os.path.relpath(path, self.base).replace(os.sep, "/")
        if relative.startswith(".."):
            return False
        return bool(self.spec.match_file(relative + "/" if is_dir else relative))


def layer_from_patterns(base: str, patterns: Iterable[str]) -> IgnoreLayer | None:
    lines = [p for p in patterns if p.strip() and not p.lstrip().startswith("#")]
    if not lines:
        return None
    return IgnoreLayer(base, GitIgnoreSpec.from_lines(lines))


def load_ignore_file(directory: str) -> IgnoreLayer | None:
    """Read ``directory/.siftignore``. Unreadable or oversized files are skipped."""
    target = os.path.join(directory, IGNORE_FILE)
    try:
        if os.path.getsize(target) > _MAX_IGNORE_BYTES:
            return None
        with open(target, encoding="utf-8", errors="replace") as handle:
            return layer_from_patterns(directory, handle.read().splitlines())
    except OSError:
        return None


def is_ignored(layers: tuple[IgnoreLayer, ...], path: str, is_dir: bool) -> bool:
    return any(layer.matches(path, is_dir) for layer in layers)
