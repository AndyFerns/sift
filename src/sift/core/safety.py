"""Path validation: deciding whether a path may be deleted.

Every check here answers one question - "is this exactly the directory the user previewed,
and is it somewhere Sift is allowed to delete?" - and refuses when the answer is anything
other than a clear yes. The checks run when a cleanup plan is built and again immediately
before each deletion.

A target is refused when it:

* is not an absolute path, or contains ``..``
* is not strictly inside the scan root
* is the scan root, a filesystem root, the home directory, or an ancestor of the home
  directory or of Sift's own config and data directories
* is a symlink, junction or other redirecting reparse point, or is reached through one
* resolves (``realpath``) to anywhere other than where it appears to be
* no longer exists, or changed from a directory to a file or back
* directly contains a ``.git`` entry
"""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass
from typing import Literal

from sift.core.config import config_dir, data_dir
from sift.core.sizing import extended_path

Kind = Literal["directory", "file"]

# Reparse tags with this bit set redirect to another location (symlinks, junctions, ...).
_NAME_SURROGATE = 0x20000000


class UnsafeOperation(Exception):
    """A cleanup step was refused. ``code`` is stable; ``reason`` is for people."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    path: str
    kind: Kind
    identity: tuple[int, int] | None
    """(device, inode). None when the filesystem does not provide a usable inode."""


def redirects(info: os.stat_result) -> bool:
    """True when an ``lstat`` result describes something that points elsewhere."""
    if stat.S_ISLNK(info.st_mode):
        return True
    if sys.platform == "win32":
        is_reparse = bool(info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return is_reparse and bool(info.st_reparse_tag & _NAME_SURROGATE)
    return False


def _split(path: str) -> list[str]:
    return [part for part in path.replace("\\", "/").split("/") if part]


def _contains(outer: str, inner: str) -> bool:
    """True when ``inner`` is ``outer`` or lies beneath it. Both must be normalised."""
    outer_c = os.path.normcase(outer)
    inner_c = os.path.normcase(inner)
    if outer_c == inner_c:
        return True
    prefix = outer_c if outer_c.endswith(os.sep) else outer_c + os.sep
    return inner_c.startswith(prefix)


def protected_paths() -> list[str]:
    """Locations that must never be deleted, nor any directory containing them."""
    candidates = [os.path.expanduser("~"), str(config_dir()), str(data_dir())]
    return [os.path.realpath(p) for p in candidates if p and os.path.isabs(p)]


def validate_target(path: str, root: str, kind: Kind) -> ValidatedTarget:
    """Check that ``path`` is safe to delete as part of a cleanup rooted at ``root``.

    Raises :class:`UnsafeOperation` with the first reason found. Read-only.
    """
    if not os.path.isabs(path) or not os.path.isabs(root):
        raise UnsafeOperation("not-absolute", "the path is not absolute")
    if ".." in _split(path):
        raise UnsafeOperation("traversal", "the path contains '..'")

    root_real = os.path.realpath(root)
    if not os.path.isdir(root_real):
        raise UnsafeOperation("root-missing", "the scan root no longer exists")
    target = os.path.normpath(path)

    if not _contains(root_real, target):
        raise UnsafeOperation("outside-root", "the path is outside the scan root")
    if os.path.normcase(target) == os.path.normcase(root_real):
        raise UnsafeOperation("is-root", "the path is the scan root itself")

    # Walk down from the root one component at a time; none may be a link.
    current = root_real
    info: os.stat_result | None = None
    parts = _split(os.path.relpath(target, root_real))
    for index, part in enumerate(parts):
        current = os.path.join(current, part)
        try:
            info = os.lstat(extended_path(current))
        except FileNotFoundError:
            raise UnsafeOperation("missing", "the path no longer exists") from None
        except OSError as exc:
            raise UnsafeOperation("unreadable", f"the path cannot be examined: {exc}") from exc
        if redirects(info):
            if index == len(parts) - 1:
                raise UnsafeOperation("link", "the path is a symlink or junction")
            raise UnsafeOperation(
                "link-in-path", "the path is reached through a symlink or junction"
            )
    assert info is not None  # target differs from the root, so there is at least one part

    # Defence in depth: whatever the OS resolves this to must be the same place.
    resolved = os.path.realpath(target)
    if os.path.normcase(resolved) != os.path.normcase(target):
        raise UnsafeOperation("link-in-path", "the path resolves to a different location")

    if os.path.dirname(resolved) == resolved:
        raise UnsafeOperation("protected", "the path is a filesystem root")
    for protected in protected_paths():
        if _contains(resolved, protected):
            raise UnsafeOperation("protected", "the path is, or contains, a protected directory")

    is_directory = stat.S_ISDIR(info.st_mode)
    if is_directory != (kind == "directory"):
        raise UnsafeOperation("wrong-kind", f"the path is no longer a {kind}")
    if is_directory and os.path.lexists(os.path.join(resolved, ".git")):
        raise UnsafeOperation("contains-repository", "the directory contains a .git entry")

    identity = (info.st_dev, info.st_ino) if info.st_ino else None
    return ValidatedTarget(target, kind, identity)
