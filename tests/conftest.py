from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest

from fixture_workspace import Workspace, build_workspace


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep every test away from the real ~/.config/sift and history database."""
    base = tmp_path_factory.mktemp("sift-user")
    patch = pytest.MonkeyPatch()
    patch.setenv("SIFT_CONFIG_DIR", str(base / "config"))
    patch.setenv("SIFT_DATA_DIR", str(base / "data"))
    yield
    patch.undo()


@pytest.fixture(scope="session")
def _workspace_template(tmp_path_factory: pytest.TempPathFactory) -> Workspace:
    return build_workspace(tmp_path_factory.mktemp("template") / "workspace")


@pytest.fixture(scope="session")
def workspace_ro(_workspace_template: Workspace) -> Workspace:
    """The shared fixture, for tests that only read it."""
    return _workspace_template


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    """A private copy of the fixture, for tests that delete things."""
    return build_workspace(tmp_path / "workspace")


def tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every path under root with its size and mtime, without following links."""
    snapshot: dict[str, tuple[int, int]] = {}
    stack = [str(root)]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                # lstat, not entry.stat(): Windows directory listings carry stale timestamps.
                stat = os.lstat(entry.path)
                snapshot[os.path.relpath(entry.path, root)] = (stat.st_size, stat.st_mtime_ns)
                # os.walk follows Windows junctions; a link's target is not part of this tree.
                is_link = entry.is_symlink() or entry.is_junction()
                if not is_link and entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
    return snapshot


def can_symlink(tmp_path: Path) -> bool:
    probe = tmp_path / "symlink-probe"
    try:
        os.symlink(tmp_path, probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        return False
    os.unlink(probe)
    return True


@pytest.fixture
def symlinks(tmp_path: Path) -> None:
    if not can_symlink(tmp_path):
        pytest.skip("creating symlinks is not permitted on this machine")


def make_junction(link: Path, target: Path) -> None:
    import _winapi

    _winapi.CreateJunction(str(target), str(link))  # type: ignore[attr-defined]


def remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
