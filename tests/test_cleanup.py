"""The cleanup engine. Every deletion here happens inside a pytest temporary directory."""

from __future__ import annotations

import os
import shutil
import stat
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import make_junction, tree_snapshot
from fixture_workspace import Workspace
from sift.core import cleanup
from sift.core.cleanup import UnknownArtifact, build_plan, cancel, execute
from sift.core.detectors import build_ruleset
from sift.core.models import (
    CleanupPlan,
    CleanupStatus,
    Confidence,
    ScanOptions,
    ScanResult,
)
from sift.core.safety import UnsafeOperation
from sift.core.scanner import scan
from sift.core.sizing import extended_path

RULES = build_ruleset()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Two real artifacts, one ambiguous directory and some source files."""
    base = tmp_path / "work"
    rust = base / "rust"
    (rust / "target" / "debug").mkdir(parents=True)
    (rust / "Cargo.toml").write_text("[package]\n")
    (rust / "src").mkdir()
    (rust / "src" / "main.rs").write_text("fn main() {}\n")
    (rust / "target" / "CACHEDIR.TAG").write_bytes(b"x" * 43)
    (rust / "target" / "debug" / "app").write_bytes(b"x" * 5000)

    node = base / "node"
    (node / "node_modules" / "dep").mkdir(parents=True)
    (node / "package.json").write_text("{}")
    (node / "index.js").write_text("// source\n")
    (node / "node_modules" / "dep" / "index.js").write_bytes(b"x" * 3000)

    (base / "misc" / "build").mkdir(parents=True)
    (base / "misc" / "build" / "important.txt").write_text("hand written")
    return base


def scanned(root: Path, **options: object) -> tuple[ScanResult, dict[str, str]]:
    result = scan(root, ScanOptions(**options))  # type: ignore[arg-type]
    return result, {a.relative_path: a.id for a in result.artifacts}


def plan_for(root: Path, *relative: str, **kwargs: object) -> tuple[CleanupPlan, ScanResult]:
    result, ids = scanned(root)
    plan = build_plan(result, [ids[r] for r in relative], RULES, **kwargs)  # type: ignore[arg-type]
    return plan, result


def refused_code(plan: CleanupPlan, token: str | None) -> str:
    with pytest.raises(UnsafeOperation) as caught:
        execute(plan, token, RULES)
    return caught.value.code


# --- building a plan --------------------------------------------------------------------


def test_plan_lists_exactly_what_was_selected(root: Path) -> None:
    plan, result = plan_for(root, "rust/target")
    assert [i.relative_path for i in plan.items] == ["rust/target"]
    item = plan.items[0]
    assert item.allowed
    assert item.refusal_reason is None
    assert Path(item.path) == root / "rust" / "target"
    assert item.logical_bytes == 5043
    assert item.file_count == 2
    assert item.identity is not None
    assert plan.root == result.root
    assert plan.scan_id == result.id
    assert plan.confirmation_token
    assert not plan.dry_run


def test_plan_totals_keep_detected_selected_and_deletable_apart(root: Path) -> None:
    plan, result = plan_for(root, "rust/target", "misc/build")
    totals = plan.totals
    assert totals.detected_count == 3
    assert totals.detected_bytes == result.totals.logical_bytes == 5043 + 3000 + 12
    assert totals.selected_count == 2
    assert totals.selected_bytes == 5043 + 12
    assert totals.deletable_count == 1
    assert totals.deletable_bytes == 5043
    assert totals.deletable_file_count == 2
    assert totals.refused_count == 1
    assert totals.refused_bytes == 12
    assert [i.relative_path for i in plan.deletable] == ["rust/target"]
    assert [i.relative_path for i in plan.refused] == ["misc/build"]


def test_building_a_plan_deletes_nothing(root: Path) -> None:
    before = tree_snapshot(root)
    plan_for(root, "rust/target", "node/node_modules", "misc/build")
    assert tree_snapshot(root) == before


def test_ambiguous_artifact_is_refused_in_the_plan(root: Path) -> None:
    plan, _ = plan_for(root, "misc/build")
    item = plan.items[0]
    assert not item.allowed
    assert item.refusal_code == "ambiguous"
    assert "ambiguous" in (item.refusal_reason or "")


def test_ambiguous_artifact_is_refused_at_every_threshold(root: Path) -> None:
    result, ids = scanned(root, min_confidence=Confidence.LOW)
    plan = build_plan(result, [ids["misc/build"]], RULES, min_confidence=Confidence.LOW)
    assert plan.items[0].refusal_code == "ambiguous"


def test_low_confidence_artifact_is_refused(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "bundle.js").write_bytes(b"x" * 10)
    result, ids = scanned(tmp_path)
    plan = build_plan(result, [ids["dist"]], RULES)
    assert plan.items[0].refusal_code == "low-confidence"

    # the user can lower the threshold on purpose; it is not ambiguous
    lenient, ids = scanned(tmp_path, min_confidence=Confidence.LOW)
    assert build_plan(lenient, [ids["dist"]], RULES).items[0].allowed


def test_stricter_threshold_at_plan_time_wins(root: Path) -> None:
    (root / "rust" / "target" / "CACHEDIR.TAG").unlink()  # now only medium confidence
    result, ids = scanned(root)
    assert build_plan(result, [ids["rust/target"]], RULES).items[0].allowed
    strict = build_plan(result, [ids["rust/target"]], RULES, min_confidence=Confidence.HIGH)
    assert strict.items[0].refusal_code == "low-confidence"


def test_unknown_artifact_id_is_an_error(root: Path) -> None:
    result, ids = scanned(root)
    with pytest.raises(UnknownArtifact) as caught:
        build_plan(result, [ids["rust/target"], "deadbeef0000"], RULES)
    assert caught.value.artifact_ids == ["deadbeef0000"]


def test_duplicate_selection_is_collapsed(root: Path) -> None:
    result, ids = scanned(root)
    plan = build_plan(result, [ids["rust/target"]] * 3, RULES)
    assert len(plan.items) == 1


def test_empty_selection_makes_an_empty_plan(root: Path) -> None:
    result, _ = scanned(root)
    plan = build_plan(result, [], RULES)
    assert plan.items == []
    assert plan.totals.deletable_count == 0


def test_artifact_changed_since_the_scan_is_refused_in_the_plan(root: Path) -> None:
    result, ids = scanned(root)
    shutil.rmtree(root / "rust" / "target")
    plan = build_plan(result, [ids["rust/target"]], RULES)
    assert plan.items[0].refusal_code == "missing"


def test_scan_result_pointing_outside_its_root_is_refused(root: Path, tmp_path: Path) -> None:
    """A tampered scan result cannot smuggle in a path outside the root."""
    victim = tmp_path / "victim" / "target"
    victim.mkdir(parents=True)
    (tmp_path / "victim" / "Cargo.toml").write_text("")
    (victim / "CACHEDIR.TAG").write_text("x")
    result, ids = scanned(root)
    artifact = result.artifact(ids["rust/target"])
    assert artifact is not None
    artifact.path = str(victim)
    plan = build_plan(result, [artifact.id], RULES)
    assert plan.items[0].refusal_code == "outside-root"
    assert victim.exists()


# --- confirmation -----------------------------------------------------------------------


def test_execute_without_confirmation_is_refused(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target", "node/node_modules")
    before = tree_snapshot(root)
    assert refused_code(plan, None) == "not-confirmed"
    assert refused_code(plan, "") == "not-confirmed"
    assert refused_code(plan, "delete") == "not-confirmed"  # the word is not the token
    assert refused_code(plan, "0" * 64) == "not-confirmed"
    assert tree_snapshot(root) == before


def test_token_from_another_plan_is_refused(root: Path) -> None:
    first, _ = plan_for(root, "rust/target")
    second, _ = plan_for(root, "node/node_modules")
    assert refused_code(second, first.confirmation_token) == "not-confirmed"
    assert (root / "node" / "node_modules").exists()


def test_plan_edited_after_the_preview_is_refused(root: Path) -> None:
    before = tree_snapshot(root)

    # add an item that was never previewed
    plan, _ = plan_for(root, "rust/target")
    other, _ = plan_for(root, "node/node_modules")
    plan.items.append(other.items[0])
    assert refused_code(plan, plan.confirmation_token) == "not-confirmed"

    # point a previewed item somewhere else
    plan, _ = plan_for(root, "rust/target")
    plan.items[0].path = str(root / "node" / "node_modules")
    assert refused_code(plan, plan.confirmation_token) == "not-confirmed"

    # force a refused item to "allowed"
    plan, _ = plan_for(root, "misc/build")
    plan.items[0].allowed = True
    assert refused_code(plan, plan.confirmation_token) == "not-confirmed"

    # turn a dry run into a real run
    plan, _ = plan_for(root, "rust/target", dry_run=True)
    plan.dry_run = False
    assert refused_code(plan, plan.confirmation_token) == "not-confirmed"

    assert tree_snapshot(root) == before


def test_expired_plan_is_refused(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cleanup, "PLAN_LIFETIME", timedelta(seconds=-1))
    plan, _ = plan_for(root, "rust/target")
    assert refused_code(plan, plan.confirmation_token) == "expired"
    assert (root / "rust" / "target").exists()


def test_plan_can_only_be_executed_once(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    execute(plan, plan.confirmation_token, RULES)
    assert refused_code(plan, plan.confirmation_token) == "already-executed"


def test_cancelling_deletes_nothing_and_is_recorded(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target", "node/node_modules")
    before = tree_snapshot(root)
    result = cancel(plan)
    assert tree_snapshot(root) == before
    assert result.cancelled
    assert not result.dry_run
    assert [i.status for i in result.items] == [CleanupStatus.CANCELLED] * 2
    assert result.totals.selected_bytes == 8043
    assert result.totals.deleted_bytes == 0
    assert result.totals.deleted_count == 0
    # backing out is final: the same preview cannot be executed afterwards
    assert refused_code(plan, plan.confirmation_token) == "already-executed"
    assert tree_snapshot(root) == before


# --- deleting ---------------------------------------------------------------------------


def test_confirmed_cleanup_deletes_exactly_the_selection(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    before = tree_snapshot(root)
    result = execute(plan, plan.confirmation_token, RULES)

    assert not (root / "rust" / "target").exists()
    after = tree_snapshot(root)
    gone = set(before) - set(after)
    assert gone == {p for p in before if Path(p).parts[:2] == ("rust", "target")}
    # everything that was not selected is byte-for-byte untouched
    assert {k: v for k, v in after.items() if k != "rust"} == {
        k: v for k, v in before.items() if k not in gone and k != "rust"
    }
    assert (root / "rust" / "src" / "main.rs").read_text() == "fn main() {}\n"
    assert (root / "node" / "node_modules" / "dep" / "index.js").exists()

    assert not result.dry_run and not result.cancelled
    item = result.items[0]
    assert item.status is CleanupStatus.DELETED
    assert item.deleted_bytes == 5043
    assert item.deleted_files == 2
    assert item.failed_bytes == 0
    assert result.totals.deleted_count == 1


def test_result_separates_detected_selected_deleted_and_refused(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target", "misc/build")
    result = execute(plan, plan.confirmation_token, RULES)
    totals = result.totals
    assert totals.detected_bytes == 8055
    assert totals.selected_bytes == 5055
    assert totals.deleted_bytes == 5043
    assert totals.refused_bytes == 12
    assert totals.failed_bytes == 0
    assert totals.would_delete_bytes == 0
    assert (totals.deleted_count, totals.failed_count, totals.refused_count) == (1, 0, 1)
    statuses = {i.relative_path: i.status for i in result.items}
    assert statuses == {
        "rust/target": CleanupStatus.DELETED,
        "misc/build": CleanupStatus.REFUSED,
    }
    assert (root / "misc" / "build" / "important.txt").read_text() == "hand written"


def test_single_file_artifact_is_deleted(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("")
    (tmp_path / ".coverage").write_bytes(b"SQLite format 3\x00" + b"\x00" * 84)
    result, ids = scanned(tmp_path)
    plan = build_plan(result, [ids[".coverage"]], RULES)
    outcome = execute(plan, plan.confirmation_token, RULES)
    assert outcome.items[0].status is CleanupStatus.DELETED
    assert outcome.items[0].deleted_bytes == 100
    assert not (tmp_path / ".coverage").exists()
    assert (tmp_path / "pyproject.toml").exists()


def test_read_only_files_are_deleted(root: Path) -> None:
    locked = root / "rust" / "target" / "debug" / "app"
    locked.chmod(stat.S_IREAD)
    plan, _ = plan_for(root, "rust/target")
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.DELETED
    assert not (root / "rust" / "target").exists()


def test_progress_is_reported_per_item(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target", "node/node_modules")
    seen: list[tuple[int, int, str]] = []
    execute(
        plan,
        plan.confirmation_token,
        RULES,
        progress=lambda index, total, item: seen.append((index, total, item.relative_path)),
    )
    assert seen == [(0, 2, plan.items[0].relative_path), (1, 2, plan.items[1].relative_path)]


def test_whole_fixture_cleanup(workspace: Workspace) -> None:
    result = scan(workspace.root, ScanOptions(recursive=True))
    expected = workspace.expect(recursive=True)
    cleanable = [a for a in result.artifacts if a.cleanable]
    review = [a for a in result.artifacts if not a.cleanable]
    assert len(cleanable) == sum(1 for e in expected.values() if e.cleanable)

    plan = build_plan(result, [a.id for a in result.artifacts], RULES)  # select everything
    assert plan.totals.deletable_count == len(cleanable)
    assert plan.totals.refused_count == len(review)
    outcome = execute(plan, plan.confirmation_token, RULES)

    for artifact in cleanable:
        assert not os.path.lexists(artifact.path), artifact.relative_path
    for artifact in review:
        assert os.path.lexists(artifact.path), artifact.relative_path
    assert outcome.totals.deleted_bytes == sum(
        e.logical_bytes for e in expected.values() if e.cleanable
    )
    assert outcome.totals.failed_bytes == 0
    assert outcome.totals.refused_count == len(review)
    # sources, manifests and the excluded project are all still there
    for kept in (
        "rust-app/Cargo.toml",
        "rust-app/src/main.rs",
        "node-app/package.json",
        "py-app/src/pkg/mod.py",
        "ignored-project/node_modules/a/index.js",
        "go-app/bin/tool",
        "ambiguous/build/out.txt",
        "conflict/target/output.bin",
        ".siftignore",
    ):
        assert (workspace.root / kept).exists(), kept
    # a second scan finds only what was left for review
    again = scan(workspace.root, ScanOptions(recursive=True))
    assert sorted(a.relative_path for a in again.artifacts) == sorted(
        a.relative_path for a in review
    )


# --- dry run ----------------------------------------------------------------------------


def test_dry_run_deletes_nothing(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target", "node/node_modules", "misc/build", dry_run=True)
    before = tree_snapshot(root)
    result = execute(plan, None, RULES)  # a dry run needs no confirmation
    assert tree_snapshot(root) == before
    assert result.dry_run
    statuses = {i.relative_path: i.status for i in result.items}
    assert statuses == {
        "rust/target": CleanupStatus.DRY_RUN,
        "node/node_modules": CleanupStatus.DRY_RUN,
        "misc/build": CleanupStatus.REFUSED,
    }
    assert result.totals.would_delete_bytes == 8043
    assert result.totals.deleted_bytes == 0
    assert result.totals.deleted_count == 0
    assert result.totals.refused_bytes == 12


def test_dry_run_token_cannot_authorise_a_real_run(root: Path) -> None:
    dry, result = plan_for(root, "rust/target", dry_run=True)
    real = build_plan(result, [dry.items[0].artifact_id], RULES)
    assert refused_code(real, dry.confirmation_token) == "not-confirmed"
    assert (root / "rust" / "target").exists()


def test_dry_run_still_validates(root: Path) -> None:
    plan, _ = plan_for(root, "node/node_modules", dry_run=True)
    (root / "node" / "package.json").unlink()
    result = execute(plan, None, RULES)
    assert result.items[0].status is CleanupStatus.REFUSED
    assert result.totals.would_delete_bytes == 0


# --- revalidation immediately before deletion -------------------------------------------


def test_directory_replaced_after_the_preview_is_refused(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    target = root / "rust" / "target"
    # Keep the old directory alive under another name so the filesystem cannot hand its
    # inode to the replacement.
    target.rename(root / "rust" / "old-target")
    target.mkdir()
    (target / "CACHEDIR.TAG").write_text("x")
    (target / "not-what-you-previewed.txt").write_text("new")

    result = execute(plan, plan.confirmation_token, RULES)
    item = result.items[0]
    assert item.status is CleanupStatus.REFUSED
    assert item.errors == ["identity-changed"]
    assert (target / "not-what-you-previewed.txt").exists()
    assert result.totals.deleted_bytes == 0


def test_evidence_removed_after_the_preview_is_refused(root: Path) -> None:
    plan, _ = plan_for(root, "node/node_modules")
    (root / "node" / "package.json").unlink()  # the only evidence it was a real node_modules
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.REFUSED
    assert result.items[0].errors == ["ambiguous"]
    assert (root / "node" / "node_modules" / "dep" / "index.js").exists()


def test_artifact_that_no_longer_matches_any_rule_is_refused(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    (root / "rust" / "Cargo.toml").unlink()
    (root / "rust" / "target" / "CACHEDIR.TAG").unlink()
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.REFUSED
    assert result.items[0].errors == ["changed"]
    assert (root / "rust" / "target" / "debug" / "app").exists()


def test_reclassified_artifact_is_refused(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    rust = root / "rust"
    (rust / "Cargo.toml").unlink()
    (rust / "target" / "CACHEDIR.TAG").unlink()
    (rust / "pom.xml").write_text("<project/>")
    (rust / "target" / "classes").mkdir()
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.REFUSED
    assert result.items[0].errors == ["changed"]
    assert "java.maven-target" in (result.items[0].message or "")
    assert (rust / "target" / "debug" / "app").exists()


def test_artifact_deleted_by_someone_else_is_reported_not_crashed(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target", "node/node_modules")
    shutil.rmtree(root / "rust" / "target")
    result = execute(plan, plan.confirmation_token, RULES)
    statuses = {i.relative_path: (i.status, i.errors) for i in result.items}
    assert statuses["rust/target"] == (CleanupStatus.REFUSED, ["missing"])
    assert statuses["node/node_modules"][0] is CleanupStatus.DELETED


def test_repository_appearing_inside_is_refused(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    (root / "rust" / "target" / ".git").mkdir()
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].errors == ["contains-repository"]
    assert (root / "rust" / "target" / "debug" / "app").exists()


@pytest.mark.usefixtures("symlinks")
def test_artifact_swapped_for_a_symlink_is_refused(root: Path, tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "CACHEDIR.TAG").write_text("x")
    (precious / "thesis.docx").write_text("years of work")
    plan, _ = plan_for(root, "rust/target")
    shutil.rmtree(root / "rust" / "target")
    os.symlink(precious, root / "rust" / "target", target_is_directory=True)

    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.REFUSED
    assert result.items[0].errors == ["link"]
    assert (precious / "thesis.docx").read_text() == "years of work"
    assert os.path.islink(root / "rust" / "target")


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_artifact_swapped_for_a_junction_is_refused(root: Path, tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "CACHEDIR.TAG").write_text("x")
    (precious / "thesis.docx").write_text("years of work")
    plan, _ = plan_for(root, "rust/target")
    shutil.rmtree(root / "rust" / "target")
    make_junction(root / "rust" / "target", precious)

    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.REFUSED
    assert result.items[0].errors == ["link"]
    assert (precious / "thesis.docx").read_text() == "years of work"


@pytest.mark.usefixtures("symlinks")
def test_parent_swapped_for_a_symlink_is_refused(root: Path, tmp_path: Path) -> None:
    decoy = tmp_path / "decoy"
    (decoy / "target").mkdir(parents=True)
    (decoy / "Cargo.toml").write_text("")
    (decoy / "target" / "CACHEDIR.TAG").write_text("x")
    (decoy / "target" / "keep.txt").write_text("keep")
    plan, _ = plan_for(root, "rust/target")
    shutil.rmtree(root / "rust")
    os.symlink(decoy, root / "rust", target_is_directory=True)

    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].errors == ["link-in-path"]
    assert (decoy / "target" / "keep.txt").exists()


# --- links inside an artifact are removed, never followed -------------------------------


@pytest.mark.usefixtures("symlinks")
def test_symlink_inside_artifact_does_not_reach_outside(root: Path, tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "thesis.docx").write_text("years of work")
    modules = root / "node" / "node_modules"
    os.symlink(precious, modules / "linked-dir", target_is_directory=True)
    os.symlink(precious / "thesis.docx", modules / "linked-file")

    plan, _ = plan_for(root, "node/node_modules")
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.DELETED
    assert not os.path.lexists(modules)
    assert (precious / "thesis.docx").read_text() == "years of work"


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_junction_inside_artifact_does_not_reach_outside(root: Path, tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "thesis.docx").write_text("years of work")
    modules = root / "node" / "node_modules"
    make_junction(modules / "linked-dir", precious)

    plan, _ = plan_for(root, "node/node_modules")
    assert plan.items[0].logical_bytes == 3000  # the junction's target is not counted
    result = execute(plan, plan.confirmation_token, RULES)
    assert result.items[0].status is CleanupStatus.DELETED
    assert result.items[0].deleted_bytes == 3000
    assert not os.path.lexists(modules)
    assert (precious / "thesis.docx").read_text() == "years of work"


# --- failures ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="open files block deletion on Windows")
def test_partial_failure_is_reported_honestly_windows(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    held = root / "rust" / "target" / "debug" / "app"
    with open(held, "rb"):  # an open handle makes the file undeletable
        result = execute(plan, plan.confirmation_token, RULES)
    item = result.items[0]
    assert item.status is CleanupStatus.PARTIAL
    assert held.exists()
    assert not (root / "rust" / "target" / "CACHEDIR.TAG").exists()
    assert item.deleted_bytes == 43
    assert item.failed_bytes == 5000
    assert item.deleted_files == 1
    assert item.errors and "app" in item.errors[0]
    assert result.totals.selected_bytes == 5043
    assert result.totals.deleted_bytes == 43
    assert result.totals.failed_bytes == 5000
    assert (result.totals.deleted_count, result.totals.failed_count) == (0, 1)


@pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX directory permissions")
@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores permission bits"
)
def test_partial_failure_is_reported_honestly_posix(root: Path) -> None:
    plan, _ = plan_for(root, "rust/target")
    debug = root / "rust" / "target" / "debug"
    debug.chmod(0o555)  # entries inside a read-only directory cannot be unlinked
    try:
        result = execute(plan, plan.confirmation_token, RULES)
    finally:
        debug.chmod(0o755)
    item = result.items[0]
    assert item.status is CleanupStatus.PARTIAL
    assert (debug / "app").exists()
    assert item.deleted_bytes == 43
    assert item.failed_bytes == 5000
    assert result.totals.failed_count == 1


def test_total_failure_is_marked_failed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan, _ = plan_for(root, "rust/target")
    monkeypatch.setattr(cleanup, "_remove", lambda path, kind: ["simulated: nothing removed"])
    result = execute(plan, plan.confirmation_token, RULES)
    item = result.items[0]
    assert item.status is CleanupStatus.FAILED
    assert item.deleted_bytes == 0
    assert item.failed_bytes == 5043
    assert item.errors == ["simulated: nothing removed"]
    assert result.totals.failed_count == 1


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_deep_paths_are_deleted(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")
    deep = tmp_path / "node_modules"
    while len(str(deep)) < 320:
        deep = deep / ("d" * 40)
    os.makedirs(extended_path(str(deep)))
    with open(extended_path(str(deep / "file.js")), "wb") as handle:
        handle.write(b"x" * 100)

    result, ids = scanned(tmp_path)
    plan = build_plan(result, [ids["node_modules"]], RULES)
    outcome = execute(plan, plan.confirmation_token, RULES)
    assert outcome.items[0].status is CleanupStatus.DELETED
    assert outcome.items[0].deleted_bytes == 100
    assert not (tmp_path / "node_modules").exists()
