"""Path validation. Nothing in this file deletes anything."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import make_junction
from sift.core.safety import UnsafeOperation, protected_paths, validate_target


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "target" / "debug").mkdir(parents=True)
    (root / "Cargo.toml").write_text("")
    (root / "target" / "CACHEDIR.TAG").write_text("x")
    (root / "target" / "debug" / "app").write_bytes(b"x" * 100)
    return root


def refusal(path: object, root: object, kind: str = "directory") -> str:
    with pytest.raises(UnsafeOperation) as caught:
        validate_target(str(path), str(root), kind)  # type: ignore[arg-type]
    assert caught.value.reason
    return caught.value.code


def test_valid_target_is_accepted(project: Path) -> None:
    target = validate_target(str(project / "target"), str(project), "directory")
    assert Path(target.path) == project / "target"
    assert target.kind == "directory"
    assert target.identity is not None
    stat = os.lstat(project / "target")
    assert target.identity == (stat.st_dev, stat.st_ino)


def test_nested_target_is_accepted(project: Path) -> None:
    validate_target(str(project / "target" / "debug"), str(project), "directory")
    validate_target(str(project / "target" / "debug" / "app"), str(project), "file")


def test_relative_path_is_refused(project: Path) -> None:
    assert refusal("target", project) == "not-absolute"
    assert refusal(project / "target", "project") == "not-absolute"


def test_dot_dot_is_refused(project: Path, tmp_path: Path) -> None:
    (tmp_path / "other" / "target").mkdir(parents=True)
    sneaky = f"{project}{os.sep}target{os.sep}..{os.sep}..{os.sep}other{os.sep}target"
    assert refusal(sneaky, project) == "traversal"
    # even when the '..' would stay inside the root
    inside = f"{project}{os.sep}target{os.sep}debug{os.sep}..{os.sep}debug"
    assert refusal(inside, project) == "traversal"
    assert refusal(f"{project}/target/../target", project) == "traversal"


def test_path_outside_root_is_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "other" / "target"
    outside.mkdir(parents=True)
    assert refusal(outside, project) == "outside-root"
    assert refusal(tmp_path, project) == "outside-root"  # the root's own parent


def test_sibling_with_same_prefix_is_outside(project: Path, tmp_path: Path) -> None:
    """``/x/project-evil`` starts with ``/x/project`` but is not inside it."""
    evil = tmp_path / "project-evil" / "target"
    evil.mkdir(parents=True)
    assert refusal(evil, project) == "outside-root"


def test_scan_root_itself_is_refused(project: Path) -> None:
    assert refusal(project, project) == "is-root"
    assert refusal(str(project) + os.sep, project) == "is-root"


def test_missing_path_is_refused(project: Path) -> None:
    assert refusal(project / "gone", project) == "missing"
    assert refusal(project / "gone" / "deeper", project) == "missing"


def test_missing_root_is_refused(tmp_path: Path) -> None:
    assert refusal(tmp_path / "nope" / "target", tmp_path / "nope") == "root-missing"


def test_kind_change_is_refused(project: Path) -> None:
    assert refusal(project / "Cargo.toml", project, "directory") == "wrong-kind"
    assert refusal(project / "target", project, "file") == "wrong-kind"


def test_directory_holding_a_repository_is_refused(project: Path) -> None:
    (project / "target" / ".git").mkdir()
    assert refusal(project / "target", project) == "contains-repository"


def test_git_file_counts_too(project: Path) -> None:
    """Worktrees and submodules use a .git *file*."""
    (project / "target" / ".git").write_text("gitdir: ../.git/worktrees/x\n")
    assert refusal(project / "target", project) == "contains-repository"


def test_filesystem_root_and_home_are_refused() -> None:
    home = Path.home().resolve()
    anchor = home.anchor
    assert refusal(anchor, anchor) == "is-root"
    assert refusal(home, anchor) == "protected"
    if home.parent != Path(anchor):
        assert refusal(home.parent, anchor) == "protected"  # an ancestor of home


def test_home_is_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "users" / "me"
    (home / "code" / "target").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    assert Path(protected_paths()[0]) == home.resolve()
    assert refusal(home, tmp_path) == "protected"
    assert refusal(tmp_path / "users", tmp_path) == "protected"
    # ordinary directories inside home are fine
    validate_target(str(home / "code" / "target"), str(tmp_path), "directory")
    validate_target(str(home / "code" / "target"), str(home), "directory")


def test_sift_own_directories_are_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "build" / "sift-config").mkdir(parents=True)
    (root / "dist" / "nested" / "sift-data").mkdir(parents=True)
    monkeypatch.setenv("SIFT_CONFIG_DIR", str(root / "build" / "sift-config"))
    monkeypatch.setenv("SIFT_DATA_DIR", str(root / "dist" / "nested" / "sift-data"))
    assert refusal(root / "build", root) == "protected"
    assert refusal(root / "dist", root) == "protected"


@pytest.mark.skipif(sys.platform != "win32", reason="case-insensitive paths")
def test_windows_paths_compare_case_insensitively(project: Path) -> None:
    shouted = str(project / "target").upper()
    target = validate_target(shouted, str(project).lower(), "directory")
    assert target.identity is not None


# --- links ------------------------------------------------------------------------------


@pytest.mark.usefixtures("symlinks")
def test_symlink_target_is_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, project / "linked", target_is_directory=True)
    assert refusal(project / "linked", project) == "link"


@pytest.mark.usefixtures("symlinks")
def test_symlink_to_file_is_refused(project: Path, tmp_path: Path) -> None:
    (tmp_path / "precious.txt").write_text("keep me")
    os.symlink(tmp_path / "precious.txt", project / ".coverage")
    assert refusal(project / ".coverage", project, "file") == "link"


@pytest.mark.usefixtures("symlinks")
def test_path_through_a_symlink_is_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "target").mkdir(parents=True)
    os.symlink(outside, project / "sub", target_is_directory=True)
    assert refusal(project / "sub" / "target", project) == "link-in-path"


@pytest.mark.usefixtures("symlinks")
def test_symlinked_root_is_resolved_first(project: Path, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    os.symlink(project, alias, target_is_directory=True)
    # paths spelled through the alias are not inside the real root
    assert refusal(alias / "target", alias) == "outside-root"
    validate_target(str(project / "target"), str(alias), "directory")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junction_target_is_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    make_junction(project / "linked", outside)
    assert refusal(project / "linked", project) == "link"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_path_through_a_junction_is_refused(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / "target").mkdir(parents=True)
    make_junction(project / "sub", outside)
    assert refusal(project / "sub" / "target", project) == "link-in-path"
