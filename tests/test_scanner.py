from __future__ import annotations

import errno
import os
import shutil
import sys
from pathlib import Path

import pytest

from conftest import make_junction, tree_snapshot
from fixture_workspace import BIG_FILES_PER_PACKAGE, BIG_PACKAGES, Expected, Workspace
from sift.core import scanner, sizing
from sift.core.config import DetectorsConfig, GoConfig
from sift.core.detectors import build_ruleset
from sift.core.models import Confidence, ScanOptions, ScanResult
from sift.core.scanner import ScanError, ScanProgress, artifact_id, scan


def by_path(result: ScanResult) -> dict[str, object]:
    return {a.relative_path: a for a in result.artifacts}


def assert_matches(result: ScanResult, expected: dict[str, Expected], root: Path) -> None:
    found = {a.relative_path: a for a in result.artifacts}
    assert sorted(found) == sorted(expected)
    for relative, want in expected.items():
        got = found[relative]
        context = f"for {relative}"
        assert got.rule == want.rule, context
        assert got.ecosystem.value == want.ecosystem, context
        assert got.confidence.value == want.confidence, context
        assert got.ambiguous is want.ambiguous, context
        assert got.cleanable is want.cleanable, context
        assert got.kind == want.kind, context
        assert got.size is not None, context
        assert got.size.complete, context
        assert got.size.logical_bytes == want.logical_bytes, context
        assert got.size.file_count == want.file_count, context
        assert Path(got.path) == root / relative, context
        assert Path(got.project_path) == (root / want.project).resolve(), context
        assert got.id == artifact_id(got.path), context
        assert got.evidence, context


# --- the shared fixture -----------------------------------------------------------------


def test_fixture_single_project_scan(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root)
    assert_matches(result, workspace_ro.expect(recursive=False), workspace_ro.root.resolve())


def test_fixture_recursive_scan(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    assert_matches(result, workspace_ro.expect(recursive=True), workspace_ro.root.resolve())


def test_fixture_with_go_configured(workspace_ro: Workspace) -> None:
    ruleset = build_ruleset(DetectorsConfig(go=GoConfig(artifact_dirs=["bin"])))
    result = scan(workspace_ro.root, ScanOptions(recursive=True), ruleset)
    assert_matches(
        result, workspace_ro.expect(recursive=True, go=True), workspace_ro.root.resolve()
    )


def test_totals_add_up(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    expected = workspace_ro.expect(recursive=True).values()
    totals = result.totals
    assert totals.artifact_count == len(expected)
    assert totals.measured_count == len(expected)
    assert totals.logical_bytes == sum(e.logical_bytes for e in expected)
    assert totals.file_count == sum(e.file_count for e in expected)
    assert totals.cleanable_count == sum(1 for e in expected if e.cleanable)
    assert totals.review_count == sum(1 for e in expected if not e.cleanable)
    assert totals.cleanable_logical_bytes == sum(e.logical_bytes for e in expected if e.cleanable)
    assert totals.cleanable_logical_bytes < totals.logical_bytes


def test_artifacts_are_sorted_largest_first(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root)
    sizes = [a.size.logical_bytes for a in result.artifacts if a.size]
    assert sizes == sorted(sizes, reverse=True)
    assert result.artifacts[0].relative_path == "big/node_modules"


def test_many_small_files_are_counted_exactly(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root)
    big = by_path(result)["big/node_modules"]
    assert big.size.file_count == BIG_PACKAGES * BIG_FILES_PER_PACKAGE + 1  # type: ignore[attr-defined]
    assert big.size.dir_count == BIG_PACKAGES * 2  # type: ignore[attr-defined]


def test_large_directory(tmp_path: Path) -> None:
    """A few thousand files across many directories, with every byte accounted for."""
    (tmp_path / "package.json").write_text("{}")
    modules = tmp_path / "node_modules"
    expected_bytes = 0
    for package in range(60):
        directory = modules / f"pkg-{package:02d}" / "dist" / "esm"
        directory.mkdir(parents=True)
        for index in range(50):
            size = (package * 7 + index) % 97
            (directory / f"chunk-{index:02d}.js").write_bytes(b"x" * size)
            expected_bytes += size
    result = scan(tmp_path)
    assert [a.relative_path for a in result.artifacts] == ["node_modules"]
    size = result.artifacts[0].size
    assert size is not None and size.complete
    assert size.file_count == 3000
    assert size.dir_count == 180
    assert size.logical_bytes == expected_bytes
    assert result.stats.directories_visited == 1  # the 180 directories inside are not walked


def test_projects_group_their_artifacts(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    projects = {p.relative_path: p for p in result.projects}
    assert {"rust-app", "node-app", "py-app", "nested/outer", "nested/outer/inner", "."} <= set(
        projects
    )
    node = projects["node-app"]
    assert node.name == "node-app"
    assert node.manifests == ["package.json"]
    assert "node" in [e.value for e in node.ecosystems]
    owned = {a.relative_path for a in result.artifacts if a.id in node.artifact_ids}
    assert owned == {
        e.relative_path
        for e in workspace_ro.expect(recursive=True).values()
        if e.project == "node-app"
    }
    assert node.logical_bytes == sum(
        e.logical_bytes
        for e in workspace_ro.expect(recursive=True).values()
        if e.project == "node-app"
    )
    assert projects["nested/outer"].is_repository
    # Every artifact belongs to exactly one project.
    listed = [i for p in result.projects for i in p.artifact_ids]
    assert sorted(listed) == sorted(a.id for a in result.artifacts)


def test_artifact_ids_are_stable_and_unique(workspace_ro: Workspace) -> None:
    first = scan(workspace_ro.root, ScanOptions(recursive=True))
    second = scan(workspace_ro.root, ScanOptions(recursive=True))
    assert first.id != second.id
    ids = [a.id for a in first.artifacts]
    assert len(set(ids)) == len(ids)
    assert {a.relative_path: a.id for a in first.artifacts} == {
        a.relative_path: a.id for a in second.artifacts
    }


def test_scanning_changes_nothing_on_disk(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    scan(workspace.root, ScanOptions(recursive=True))
    assert tree_snapshot(workspace.root) == before


# --- nested repositories ----------------------------------------------------------------


def test_nested_repository_is_skipped_unless_recursive(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root)
    assert not [a for a in result.artifacts if a.relative_path.startswith("nested/")]
    skipped = [i for i in result.issues if i.kind == "nested-repository"]
    assert [Path(i.path) for i in skipped] == [workspace_ro.root.resolve() / "nested" / "outer"]


def test_recursive_scan_enters_nested_repositories(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    found = by_path(result)
    assert "nested/outer/node_modules" in found
    assert "nested/outer/inner/target" in found
    assert not [i for i in result.issues if i.kind == "nested-repository"]


def test_scan_root_may_itself_be_a_repository(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root / "nested" / "outer")
    # outer is scanned as the project; inner is a nested repo and is left alone
    assert sorted(by_path(result)) == ["node_modules"]
    assert [i.kind for i in result.issues] == ["nested-repository"]


def test_version_control_directories_are_never_entered(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    hidden = tmp_path / ".git" / "modules" / "sub"
    hidden.mkdir(parents=True)
    (hidden / "package.json").write_text("{}")
    (hidden / "node_modules").mkdir()
    result = scan(tmp_path, ScanOptions(recursive=True))
    assert result.artifacts == []


# --- artifacts are opaque ---------------------------------------------------------------


def test_nothing_inside_an_artifact_is_reported(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    paths = [a.relative_path for a in result.artifacts]
    for outer in paths:
        inner = [p for p in paths if p.startswith(outer + "/")]
        assert not inner, f"{inner} reported inside artifact {outer}"
    assert "node-app/node_modules/left-pad/node_modules" not in paths


def test_discovery_does_not_walk_artifact_contents(
    workspace_ro: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed: list[str] = []
    real = scanner.list_dir

    def spy(path: str, **kwargs: object) -> object:
        listed.append(os.path.relpath(path, workspace_ro.root.resolve()).replace(os.sep, "/"))
        return real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scanner, "list_dir", spy)
    result = scan(workspace_ro.root, ScanOptions(measure=False))
    # The nested repository is listed once (to notice its .git) and then left alone.
    assert result.stats.directories_visited == len(listed) - 1
    assert not [p for p in listed if "node_modules" in p or "target" in p.split("/")]
    assert len(listed) < 40  # the fixture holds 150+ files, almost all inside artifacts


# --- exclusions -------------------------------------------------------------------------


def test_siftignore_excludes_a_project(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    assert not [a for a in result.artifacts if "ignored-project" in a.relative_path]
    assert result.stats.excluded == 1


def test_without_siftignore_the_project_is_found(workspace: Workspace) -> None:
    (workspace.root / ".siftignore").unlink()
    result = scan(workspace.root)
    assert "ignored-project/node_modules" in by_path(result)


def test_config_exclude_patterns(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(exclude=["node-app/", "**/target/"]))
    paths = sorted(by_path(result))
    assert not [p for p in paths if p.startswith("node-app/")]
    assert not [p for p in paths if p.endswith("/target")]
    assert "py-app/.venv" in paths


def test_exclude_can_target_a_single_file_artifact(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(exclude=[".coverage"]))
    assert "py-app/.coverage" not in by_path(result)


def test_nested_siftignore_only_applies_below_itself(tmp_path: Path) -> None:
    for name in ("a", "b"):
        project = tmp_path / name
        (project / "node_modules" / "dep").mkdir(parents=True)
        (project / "package.json").write_text("{}")
    (tmp_path / "a" / ".siftignore").write_text("node_modules/\n")
    result = scan(tmp_path)
    assert sorted(by_path(result)) == ["b/node_modules"]


def test_siftignore_comments_and_blank_lines(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / ".siftignore").write_text("# only a comment\n\n")
    assert sorted(by_path(scan(tmp_path))) == ["node_modules"]


# --- depth ------------------------------------------------------------------------------


def test_depth_zero_only_examines_the_root(workspace_ro: Workspace) -> None:
    assert scan(workspace_ro.root, ScanOptions(max_depth=0)).artifacts == []
    result = scan(workspace_ro.root / "rust-app", ScanOptions(max_depth=0))
    assert sorted(by_path(result)) == ["target"]


def test_depth_limits_how_far_the_walk_descends(workspace_ro: Workspace) -> None:
    shallow = sorted(by_path(scan(workspace_ro.root, ScanOptions(max_depth=1))))
    assert "rust-app/target" in shallow
    assert "py-app/src/pkg/__pycache__" not in shallow
    assert "node-app/.angular/cache" in shallow  # found from node-app, one level down
    assert all(p.count("/") <= 2 for p in shallow)

    deep = sorted(by_path(scan(workspace_ro.root, ScanOptions(max_depth=3))))
    assert "py-app/src/pkg/__pycache__" in deep


# --- links ------------------------------------------------------------------------------


def test_symlinks_are_not_followed(workspace_ro: Workspace) -> None:
    if not workspace_ro.symlinks:
        pytest.skip("creating symlinks is not permitted on this machine")
    result = scan(workspace_ro.root, ScanOptions(recursive=True))
    assert not [a for a in result.artifacts if a.relative_path.startswith("links/")]
    assert result.stats.links_skipped >= 3
    assert not [i for i in result.issues if "links" in i.path]


@pytest.mark.usefixtures("symlinks")
def test_symlinked_artifact_is_not_classified(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere" / "node_modules"
    (real / "dep").mkdir(parents=True)
    (real / "dep" / "index.js").write_bytes(b"x" * 500)
    project = tmp_path / "project"
    project.mkdir()
    (project / "package.json").write_text("{}")
    os.symlink(real, project / "node_modules", target_is_directory=True)

    result = scan(project)
    assert result.artifacts == []
    assert result.stats.links_skipped == 1


@pytest.mark.usefixtures("symlinks")
def test_broken_symlink_is_harmless(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    os.symlink(tmp_path / "missing", tmp_path / "dangling")
    os.symlink(tmp_path / "missing", tmp_path / "node_modules" / "dangling")
    result = scan(tmp_path)
    assert sorted(by_path(result)) == ["node_modules"]
    artifact = result.artifacts[0]
    assert artifact.size is not None
    assert artifact.size.link_count == 1
    assert artifact.size.complete
    assert result.issues == []


@pytest.mark.usefixtures("symlinks")
def test_symlink_inside_artifact_is_not_measured_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"x" * 50_000)
    project = tmp_path / "project"
    (project / "node_modules" / "dep").mkdir(parents=True)
    (project / "package.json").write_text("{}")
    (project / "node_modules" / "dep" / "index.js").write_bytes(b"x" * 10)
    os.symlink(outside, project / "node_modules" / "escape", target_is_directory=True)

    artifact = scan(project).artifacts[0]
    assert artifact.size is not None
    assert artifact.size.logical_bytes == 10
    assert artifact.size.link_count == 1


@pytest.mark.usefixtures("symlinks")
def test_symlinked_scan_root_is_resolved(workspace_ro: Workspace, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    os.symlink(workspace_ro.root / "rust-app", alias, target_is_directory=True)
    result = scan(alias)
    assert Path(result.root) == (workspace_ro.root / "rust-app").resolve()
    assert Path(result.artifacts[0].path) == (workspace_ro.root / "rust-app" / "target").resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junctions_are_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "node_modules"
    (outside / "dep").mkdir(parents=True)
    (outside / "dep" / "big.bin").write_bytes(b"x" * 50_000)
    project = tmp_path / "project"
    (project / "sub").mkdir(parents=True)
    (project / "package.json").write_text("{}")
    make_junction(project / "node_modules", outside)  # a junction named like an artifact
    make_junction(project / "sub" / "loop", project)  # a junction that would recurse forever

    result = scan(project, ScanOptions(recursive=True))
    assert result.artifacts == []
    assert result.stats.links_skipped == 2


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junction_inside_artifact_is_not_measured_through(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"x" * 50_000)
    project = tmp_path / "project"
    (project / "node_modules" / "dep").mkdir(parents=True)
    (project / "package.json").write_text("{}")
    (project / "node_modules" / "dep" / "index.js").write_bytes(b"x" * 10)
    make_junction(project / "node_modules" / "escape", outside)

    artifact = scan(project).artifacts[0]
    assert artifact.size is not None
    assert artifact.size.logical_bytes == 10
    assert artifact.size.link_count == 1


def test_fixture_junction_is_skipped(workspace_ro: Workspace) -> None:
    if not workspace_ro.junctions:
        pytest.skip("junctions are a Windows feature")
    result = scan(workspace_ro.root)
    assert not [a for a in result.artifacts if a.relative_path.startswith("links/")]
    assert result.stats.links_skipped >= 1


# --- unreadable and disappearing paths --------------------------------------------------


def _fail_listing(
    monkeypatch: pytest.MonkeyPatch, victim: Path, error: type[OSError], code: int = errno.EACCES
) -> None:
    real = scanner.list_dir

    def flaky(path: str, **kwargs: object) -> object:
        if Path(path) == victim:
            raise error(code, "simulated", path)
        return real(path, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scanner, "list_dir", flaky)


def test_permission_error_is_reported_and_scan_continues(
    workspace_ro: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = workspace_ro.root.resolve() / "py-app"
    _fail_listing(monkeypatch, victim, PermissionError)
    result = scan(workspace_ro.root)
    issues = [i for i in result.issues if i.kind == "permission"]
    assert [Path(i.path) for i in issues] == [victim]
    paths = by_path(result)
    assert not [p for p in paths if p.startswith("py-app/")]
    assert "rust-app/target" in paths


def test_directory_vanishing_during_discovery(
    workspace_ro: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = workspace_ro.root.resolve() / "java-maven"
    _fail_listing(monkeypatch, victim, FileNotFoundError)
    result = scan(workspace_ro.root)
    assert [(i.kind, Path(i.path)) for i in result.issues if i.kind == "vanished"] == [
        ("vanished", victim)
    ]
    assert "java-gradle/build" in by_path(result)


def test_other_os_errors_are_reported(
    workspace_ro: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = workspace_ro.root.resolve() / "cpp-app"
    _fail_listing(monkeypatch, victim, OSError, errno.EIO)
    result = scan(workspace_ro.root)
    assert [Path(i.path) for i in result.issues if i.kind == "error"] == [victim]


def test_artifact_vanishing_before_it_is_measured(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    doomed = workspace.root.resolve() / "rust-app" / "target"
    real = sizing.measure_all

    def delete_then_measure(targets: object, **kwargs: object) -> object:
        shutil.rmtree(doomed)
        return real(targets, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sizing, "measure_all", delete_then_measure)
    result = scan(workspace.root)
    artifact = by_path(result)["rust-app/target"]
    assert artifact.size is not None  # type: ignore[attr-defined]
    assert not artifact.size.complete  # type: ignore[attr-defined]
    assert artifact.size.logical_bytes == 0  # type: ignore[attr-defined]
    assert [(i.kind, Path(i.path)) for i in result.issues if i.kind == "vanished"] == [
        ("vanished", doomed)
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="chmod does not restrict reads on Windows")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
)
def test_real_unreadable_directory(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    locked = tmp_path / "locked"
    (locked / "node_modules").mkdir(parents=True)
    inner = tmp_path / "node_modules" / "private"
    inner.mkdir()
    (inner / "secret.js").write_bytes(b"x" * 100)
    locked.chmod(0o000)
    inner.chmod(0o000)
    try:
        result = scan(tmp_path)
    finally:
        locked.chmod(0o755)
        inner.chmod(0o755)
    assert [Path(i.path) for i in result.issues if i.kind == "permission"] == [locked]
    artifact = by_path(result)["node_modules"]
    assert not artifact.size.complete  # type: ignore[attr-defined]
    assert artifact.size.error_count == 1  # type: ignore[attr-defined]


def test_missing_root_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ScanError, match="does not exist"):
        scan(tmp_path / "nope")


def test_root_must_be_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x")
    with pytest.raises(ScanError, match="not a directory"):
        scan(target)


def test_empty_directory(tmp_path: Path) -> None:
    result = scan(tmp_path)
    assert result.artifacts == []
    assert result.projects == []
    assert result.totals.artifact_count == 0
    assert result.totals.allocated_bytes is None


# --- thresholds, budgets, progress ------------------------------------------------------


def test_confidence_threshold_controls_cleanable(workspace_ro: Workspace) -> None:
    strict = by_path(scan(workspace_ro.root, ScanOptions(min_confidence=Confidence.HIGH)))
    default = by_path(scan(workspace_ro.root))
    lenient = by_path(scan(workspace_ro.root, ScanOptions(min_confidence=Confidence.LOW)))

    # node-app/dist is low confidence but not ambiguous
    assert not strict["node-app/dist"].cleanable  # type: ignore[attr-defined]
    assert not default["node-app/dist"].cleanable  # type: ignore[attr-defined]
    assert lenient["node-app/dist"].cleanable  # type: ignore[attr-defined]
    assert "threshold" in default["node-app/dist"].review_reason  # type: ignore[attr-defined]

    # ambiguous artifacts are never cleanable, whatever the threshold
    for name in ("ambiguous/build", "ambiguous/target", "py-app/venv", "conflict/target"):
        assert not lenient[name].cleanable  # type: ignore[attr-defined]
        assert lenient[name].review_reason  # type: ignore[attr-defined]


def test_medium_confidence_needs_medium_threshold(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("")
    (tmp_path / "target" / "debug").mkdir(parents=True)
    assert scan(tmp_path).artifacts[0].cleanable
    assert not scan(tmp_path, ScanOptions(min_confidence=Confidence.HIGH)).artifacts[0].cleanable


def test_exhausted_time_budget_reports_not_measured(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(time_budget_seconds=1e-9))
    directories = [a for a in result.artifacts if a.kind == "directory"]
    assert directories
    assert all(a.size is None for a in directories), "a partial walk must never be reported"
    unmeasured = [i for i in result.issues if i.kind == "not-measured"]
    assert len(unmeasured) == len(directories)
    assert result.totals.measured_count == len(result.artifacts) - len(directories)
    # classification does not depend on sizing
    assert sorted(by_path(result)) == sorted(workspace_ro.expect(recursive=False))


def test_generous_time_budget_measures_everything(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(time_budget_seconds=60))
    assert all(a.size is not None for a in result.artifacts)


def test_measuring_can_be_disabled(workspace_ro: Workspace) -> None:
    result = scan(workspace_ro.root, ScanOptions(measure=False))
    assert all(a.size is None for a in result.artifacts)
    assert not [i for i in result.issues if i.kind == "not-measured"]
    assert result.totals.logical_bytes == 0


def test_progress_is_reported(workspace_ro: Workspace) -> None:
    events: list[ScanProgress] = []
    result = scan(workspace_ro.root, progress=events.append)
    phases = [e.phase for e in events]
    assert phases[-1] == "done"
    assert phases.count("sizing") == len(result.artifacts)
    assert events[-1].artifacts_found == len(result.artifacts)
    assert events[-1].artifacts_measured == len(result.artifacts)


def test_stats_record_timings(workspace_ro: Workspace) -> None:
    stats = scan(workspace_ro.root).stats
    assert stats.directories_visited > 0
    assert stats.total_ms >= stats.discovery_ms >= 0
    assert stats.total_ms >= stats.sizing_ms >= 0


def test_names_that_are_not_valid_unicode_are_skipped_not_fatal(tmp_path: Path) -> None:
    """Such a path cannot be reported exactly, so it must never become a cleanup target."""
    odd = tmp_path / "odd-\udcff-project"
    try:
        (odd / "node_modules" / "dep").mkdir(parents=True)
        (odd / "package.json").write_text("{}")
    except (OSError, UnicodeError):
        pytest.skip("this filesystem refuses names that are not valid Unicode")
    normal = tmp_path / "normal"
    (normal / "node_modules" / "dep").mkdir(parents=True)
    (normal / "package.json").write_text("{}")

    result = scan(tmp_path)
    assert sorted(by_path(result)) == ["normal/node_modules"]
    skipped = [i for i in result.issues if "not valid Unicode" in i.message]
    assert len(skipped) == 1
    assert skipped[0].kind == "error"
    result.model_dump_json()  # the whole result must still serialise
