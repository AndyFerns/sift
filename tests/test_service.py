from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import tree_snapshot
from fixture_workspace import Workspace
from sift.core.config import SiftConfig, load_config
from sift.core.models import CleanupStatus, Confidence
from sift.core.service import (
    ArtifactNotFound,
    PathNotAllowed,
    PlanNotFound,
    ScanError,
    ScanNotFound,
    SiftService,
    UnsafeOperation,
)


def ids(service: SiftService, root: Path, **options: object) -> tuple[object, dict[str, str]]:
    result = service.scan(root, service.scan_options(**options))  # type: ignore[arg-type]
    return result, {a.relative_path: a.id for a in result.artifacts}


# --- scanning and history ---------------------------------------------------------------


def test_scan_matches_the_fixture(workspace_ro: Workspace) -> None:
    service = SiftService()
    result = service.scan(workspace_ro.root, service.scan_options(recursive=True))
    expected = workspace_ro.expect(recursive=True)
    assert {a.relative_path: a.rule for a in result.artifacts} == {
        k: v.rule for k, v in expected.items()
    }
    assert result.totals.logical_bytes == sum(e.logical_bytes for e in expected.values())
    assert result.options.detectors == ["rust", "node", "python", "java", "cpp", "go", "general"]


def test_scans_are_remembered_and_recorded(workspace_ro: Workspace) -> None:
    service = SiftService()
    assert service.latest_scan() is None
    first = service.scan(workspace_ro.root / "rust-app")
    second = service.scan(workspace_ro.root / "node-app")
    assert service.get_scan(first.id) == first
    assert service.latest_scan() == second
    assert [s.id for s in service.list_scans()] == [second.id, first.id]

    # a new process sees the same history
    later = SiftService()
    assert later.get_scan(first.id) == first
    assert later.latest_scan() == second
    with pytest.raises(ScanNotFound):
        later.get_scan("nope")


def test_record_false_keeps_a_scan_out_of_history(workspace_ro: Workspace) -> None:
    service = SiftService()
    result = service.scan(workspace_ro.root / "rust-app", record=False)
    assert service.get_scan(result.id) == result
    assert SiftService().list_scans() == []


def test_history_can_be_disabled(workspace_ro: Workspace) -> None:
    config = SiftConfig()
    config.history.enabled = False
    service = SiftService(config)
    assert service.history is None
    result = service.scan(workspace_ro.root / "rust-app")
    assert [s.id for s in service.list_scans()] == [result.id]  # this session only
    assert service.list_cleanups() == []
    assert SiftService(config).list_scans() == []
    with pytest.raises(ArtifactNotFound):
        SiftService(config).inspect(result.artifacts[0].id)


def test_scan_options_start_from_the_config() -> None:
    config = SiftConfig()
    config.scan.exclude = ["vendor/"]
    config.scan.max_depth = 4
    config.scan.min_confidence = Confidence.HIGH
    config.scan.time_budget_seconds = 2.0
    service = SiftService(config)

    defaults = service.scan_options()
    assert defaults.exclude == ["vendor/"]
    assert defaults.max_depth == 4
    assert defaults.min_confidence is Confidence.HIGH
    assert defaults.time_budget_seconds == 2.0
    assert defaults.recursive is False

    chosen = service.scan_options(
        recursive=True,
        max_depth=1,
        exclude=["tmp/"],
        min_confidence=Confidence.LOW,
        time_budget=9.0,
    )
    assert chosen.exclude == ["vendor/", "tmp/"]
    assert (chosen.max_depth, chosen.min_confidence, chosen.time_budget_seconds) == (
        1,
        Confidence.LOW,
        9.0,
    )
    assert chosen.recursive is True


def test_configured_detectors_are_used(workspace_ro: Workspace) -> None:
    config = SiftConfig()
    config.detectors.go.artifact_dirs = ["bin"]
    config.detectors.enabled = ["go"]
    result = SiftService(config).scan(workspace_ro.root)
    assert [a.relative_path for a in result.artifacts] == ["go-app/bin"]


def test_missing_path_is_a_scan_error(tmp_path: Path) -> None:
    with pytest.raises(ScanError):
        SiftService().scan(tmp_path / "nope")


def test_exports(workspace_ro: Workspace) -> None:
    service = SiftService()
    result = service.scan(workspace_ro.root / "rust-app")
    assert service.export_scan(result, "json").lstrip().startswith("{")
    assert service.export_scan(result, "csv").startswith("id,relative_path,")


# --- inspecting -------------------------------------------------------------------------


def test_inspect_reports_what_is_on_disk(workspace_ro: Workspace) -> None:
    service = SiftService()
    _, found = ids(service, workspace_ro.root)
    detail = service.inspect(found["rust-app/target"])
    assert detail.artifact.relative_path == "rust-app/target"
    assert detail.exists and detail.still_matches
    assert detail.current_size is not None
    assert detail.current_size.logical_bytes == 16307
    assert detail.cleanup_allowed and detail.cleanup_refusal is None
    assert [(c.name, c.kind, c.logical_bytes, c.file_count) for c in detail.children] == [
        ("debug", "directory", 16144, 3),
        (".rustc_info.json", "file", 120, 1),
        ("CACHEDIR.TAG", "file", 43, 1),
    ]


def test_inspect_explains_why_cleanup_is_refused(workspace_ro: Workspace) -> None:
    service = SiftService()
    _, found = ids(service, workspace_ro.root)
    ambiguous = service.inspect(found["ambiguous/build"])
    assert not ambiguous.cleanup_allowed
    assert "ambiguous" in (ambiguous.cleanup_refusal or "")
    low = service.inspect(found["node-app/dist"])
    assert not low.cleanup_allowed
    assert "threshold" in (low.cleanup_refusal or "")


def test_inspect_a_single_file_artifact(workspace_ro: Workspace) -> None:
    service = SiftService()
    _, found = ids(service, workspace_ro.root)
    detail = service.inspect(found["py-app/.coverage"])
    assert detail.artifact.kind == "file"
    assert detail.children == []
    assert detail.current_size is not None and detail.current_size.logical_bytes == 4096


def test_inspect_notices_changes_since_the_scan(workspace: Workspace) -> None:
    service = SiftService()
    _, found = ids(service, workspace.root)

    (workspace.root / "rust-app" / "target" / "extra.bin").write_bytes(b"x" * 1000)
    grown = service.inspect(found["rust-app/target"])
    assert grown.artifact.size is not None and grown.artifact.size.logical_bytes == 16307
    assert grown.current_size is not None and grown.current_size.logical_bytes == 17307

    (workspace.root / "node-app" / "package.json").unlink()
    weaker = service.inspect(found["node-app/.nuxt"])
    assert weaker.exists and not weaker.still_matches and not weaker.cleanup_allowed

    shutil.rmtree(workspace.root / "java-maven" / "target")
    gone = service.inspect(found["java-maven/target"])
    assert not gone.exists and not gone.still_matches
    assert gone.current_size is None and gone.children == []
    assert not gone.cleanup_allowed


def test_inspect_finds_artifacts_from_earlier_sessions(workspace_ro: Workspace) -> None:
    _, found = ids(SiftService(), workspace_ro.root)
    detail = SiftService().inspect(found["node-app/node_modules"])
    assert detail.artifact.rule == "node.node_modules"


def test_inspect_prefers_the_most_recent_scan(workspace_ro: Workspace) -> None:
    service = SiftService()
    whole, found = ids(service, workspace_ro.root)
    part = service.scan(workspace_ro.root / "rust-app")
    assert service.inspect(found["rust-app/target"]).scan_id == part.id
    assert service.inspect(found["rust-app/target"], whole.id).scan_id == whole.id  # type: ignore[attr-defined]
    with pytest.raises(ArtifactNotFound):
        service.inspect(found["node-app/.next"], part.id)
    with pytest.raises(ArtifactNotFound):
        service.inspect("000000000000")


def test_inspecting_changes_nothing(workspace: Workspace) -> None:
    service = SiftService()
    _, found = ids(service, workspace.root)
    before = tree_snapshot(workspace.root)
    for artifact_id in found.values():
        service.inspect(artifact_id)
    assert tree_snapshot(workspace.root) == before


# --- scan roots -------------------------------------------------------------------------


def test_roots_come_from_config_and_arguments(tmp_path: Path) -> None:
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
    config = SiftConfig()
    config.scan.roots = [str(tmp_path / "a"), str(tmp_path / "missing")]
    service = SiftService(config, roots=[str(tmp_path / "b"), str(tmp_path / "a")])
    assert [Path(r) for r in service.roots] == [
        (tmp_path / "b").resolve(),
        (tmp_path / "a").resolve(),
    ]


def test_paths_must_be_inside_a_root(tmp_path: Path) -> None:
    (tmp_path / "root" / "project").mkdir(parents=True)
    (tmp_path / "root-other").mkdir()
    (tmp_path / "elsewhere").mkdir()
    service = SiftService(roots=[str(tmp_path / "root")])
    assert Path(service.resolve_allowed(tmp_path / "root")) == (tmp_path / "root").resolve()
    assert service.resolve_allowed(tmp_path / "root" / "project").endswith("project")
    for outside in (tmp_path / "elsewhere", tmp_path / "root-other", tmp_path, Path.home()):
        with pytest.raises(PathNotAllowed):
            service.resolve_allowed(outside)
    with pytest.raises(PathNotAllowed):
        service.resolve_allowed(tmp_path / "root" / ".." / "elsewhere")
    with pytest.raises(ScanError):
        service.resolve_allowed(tmp_path / "root" / "missing")


def test_no_roots_means_nothing_is_allowed(tmp_path: Path) -> None:
    service = SiftService()
    assert service.roots == []
    with pytest.raises(PathNotAllowed):
        service.resolve_allowed(tmp_path)


# --- cleanup ----------------------------------------------------------------------------


def test_cleanup_goes_through_preview_and_confirmation(workspace: Workspace) -> None:
    service = SiftService()
    scan, found = ids(service, workspace.root)
    plan = service.plan_cleanup(scan, [found["rust-app/target"], found["ambiguous/build"]])  # type: ignore[arg-type]
    assert service.get_plan(plan.id) is plan
    assert (workspace.root / "rust-app" / "target").exists()  # a preview deletes nothing

    with pytest.raises(UnsafeOperation):
        service.execute_cleanup(plan)  # no confirmation
    assert (workspace.root / "rust-app" / "target").exists()

    result = service.execute_cleanup(plan, plan.confirmation_token)
    assert not (workspace.root / "rust-app" / "target").exists()
    assert (workspace.root / "ambiguous" / "build").exists()
    assert {i.relative_path: i.status for i in result.items} == {
        "rust-app/target": CleanupStatus.DELETED,
        "ambiguous/build": CleanupStatus.REFUSED,
    }
    with pytest.raises(PlanNotFound):
        service.get_plan(plan.id)

    audit = SiftService().list_cleanups()
    assert [(a.id, a.deleted_bytes, a.dry_run, a.cancelled) for a in audit] == [
        (result.id, 16307, False, False)
    ]
    assert SiftService().get_cleanup(result.id) == result


def test_cancelled_cleanup_is_audited_and_deletes_nothing(workspace: Workspace) -> None:
    service = SiftService()
    scan, found = ids(service, workspace.root)
    before = tree_snapshot(workspace.root)
    plan = service.plan_cleanup(scan, [found["rust-app/target"]])  # type: ignore[arg-type]
    result = service.cancel_cleanup(plan)
    assert result.cancelled
    assert tree_snapshot(workspace.root) == before
    assert [(a.cancelled, a.deleted_bytes) for a in service.list_cleanups()] == [(True, 0)]
    with pytest.raises(PlanNotFound):
        service.get_plan(plan.id)


def test_dry_run_through_the_service(workspace: Workspace) -> None:
    service = SiftService()
    scan, found = ids(service, workspace.root)
    before = tree_snapshot(workspace.root)
    plan = service.plan_cleanup(scan, list(found.values()), dry_run=True)  # type: ignore[arg-type]
    result = service.execute_cleanup(plan)
    assert result.dry_run
    assert tree_snapshot(workspace.root) == before
    assert result.totals.would_delete_bytes == scan.totals.cleanable_logical_bytes  # type: ignore[attr-defined]
    assert service.list_cleanups()[0].dry_run


# --- changing configuration -------------------------------------------------------------


def test_apply_config_rebuilds_detectors_and_saves(workspace_ro: Workspace, tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    service = SiftService(config_file=target)
    assert "go-app/bin" not in {a.relative_path for a in service.scan(workspace_ro.root).artifacts}

    updated = service.config.model_copy(deep=True)
    updated.detectors.go.artifact_dirs = ["bin"]
    updated.history.enabled = False
    service.apply_config(updated)

    assert "go-app/bin" in {a.relative_path for a in service.scan(workspace_ro.root).artifacts}
    assert service.history is None
    assert load_config(target) == updated
    assert SiftService(config_file=target).config == updated
