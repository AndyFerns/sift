from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from fixture_workspace import Workspace
from sift.core import reports
from sift.core.cleanup import build_plan, cancel, execute
from sift.core.detectors import build_ruleset
from sift.core.history import History
from sift.core.models import ScanOptions, ScanResult
from sift.core.reports import format_bytes
from sift.core.scanner import scan

RULES = build_ruleset()
SHARED = Path(__file__).parent / "shared" / "format_bytes.json"


def render(renderable: object) -> str:
    console = Console(width=200, record=True, color_system=None, force_terminal=False)
    console.print(renderable)
    return console.export_text()


@pytest.fixture(scope="module")
def result(workspace_ro: Workspace) -> ScanResult:
    return scan(workspace_ro.root, ScanOptions(recursive=True))


# --- sizes ------------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "text"), json.loads(SHARED.read_text()))
def test_format_bytes_matches_the_shared_table(value: int, text: str) -> None:
    """The dashboard formats sizes from this same table; see web/src/lib/format.test.ts."""
    assert format_bytes(value) == text


def test_format_bytes_none() -> None:
    assert format_bytes(None) == "-"


# --- JSON and CSV -----------------------------------------------------------------------


def test_json_export_round_trips(result: ScanResult) -> None:
    text = reports.scan_to_json(result)
    assert ScanResult.model_validate_json(text) == result
    data = json.loads(text)
    assert data["totals"]["artifact_count"] == len(result.artifacts)
    assert {"id", "path", "rule", "confidence", "ambiguous", "cleanable", "size"} <= set(
        data["artifacts"][0]
    )


def test_csv_export_has_one_row_per_artifact(result: ScanResult) -> None:
    rows = list(csv.DictReader(io.StringIO(reports.scan_to_csv(result))))
    assert len(rows) == len(result.artifacts)
    assert tuple(rows[0].keys()) == reports.CSV_COLUMNS
    by_path = {row["relative_path"]: row for row in rows}
    for artifact in result.artifacts:
        row = by_path[artifact.relative_path]
        assert row["id"] == artifact.id
        assert row["rule"] == artifact.rule
        assert row["confidence"] == artifact.confidence.value
        assert row["ambiguous"] == str(artifact.ambiguous).lower()
        assert row["cleanable"] == str(artifact.cleanable).lower()
        assert artifact.size is not None
        assert int(row["logical_bytes"]) == artifact.size.logical_bytes
        assert int(row["file_count"]) == artifact.size.file_count
    assert sum(int(r["logical_bytes"]) for r in rows) == result.totals.logical_bytes


def test_csv_marks_unmeasured_artifacts(workspace_ro: Workspace) -> None:
    unmeasured = scan(workspace_ro.root, ScanOptions(measure=False))
    rows = list(csv.DictReader(io.StringIO(reports.scan_to_csv(unmeasured))))
    assert {row["measured"] for row in rows} == {"false"}
    assert {row["logical_bytes"] for row in rows} == {""}


# --- terminal ---------------------------------------------------------------------------


def test_scan_report_lists_every_artifact(result: ScanResult) -> None:
    text = render(reports.scan_report(result))
    for artifact in result.artifacts:
        assert artifact.id in text
        assert artifact.relative_path in text
    assert f"{result.totals.artifact_count} artifacts" in text
    assert format_bytes(result.totals.logical_bytes) in text
    assert format_bytes(result.totals.cleanable_logical_bytes) in text
    assert "need review" in text
    assert "cleanable" in text


def test_scan_report_for_an_empty_scan(tmp_path: Path) -> None:
    text = render(reports.scan_report(scan(tmp_path)))
    assert "No development artifacts found." in text


def test_scan_report_mentions_skipped_repositories(workspace_ro: Workspace) -> None:
    text = render(reports.scan_report(scan(workspace_ro.root)))
    assert "nested-repository" in text


def test_scan_report_says_when_sizes_are_missing(workspace_ro: Workspace) -> None:
    text = render(reports.scan_report(scan(workspace_ro.root, ScanOptions(measure=False))))
    assert "not measured" in text


def test_plan_and_cleanup_reports_keep_the_sizes_apart(workspace: Workspace) -> None:
    found = scan(workspace.root)
    ids = {a.relative_path: a.id for a in found.artifacts}
    plan = build_plan(found, [ids["rust-app/target"], ids["ambiguous/build"]], RULES)
    preview = render(reports.plan_report(plan))
    assert "Will delete 1 artifact(s)" in preview
    assert str(workspace.root.resolve() / "rust-app" / "target") in preview
    assert "Refused" in preview
    assert "ambiguous" in preview
    for label in ("detected", "selected", "to delete"):
        assert label in preview

    outcome = render(reports.cleanup_report(execute(plan, plan.confirmation_token, RULES)))
    for label in ("detected", "selected", "deleted", "failed", "refused"):
        assert label in outcome
    assert format_bytes(16307) in outcome  # the rust target directory
    assert "reversible" not in (preview + outcome).lower()
    assert "undo" not in (preview + outcome).lower()


def test_dry_run_and_cancel_reports_say_nothing_was_deleted(workspace_ro: Workspace) -> None:
    found = scan(workspace_ro.root)
    target = next(a for a in found.artifacts if a.relative_path == "rust-app/target")
    dry = build_plan(found, [target.id], RULES, dry_run=True)
    assert "Would delete" in render(reports.plan_report(dry))
    assert "Dry run - nothing was deleted." in render(
        reports.cleanup_report(execute(dry, None, RULES))
    )
    real = build_plan(found, [target.id], RULES)
    assert "Cancelled - nothing was deleted." in render(reports.cleanup_report(cancel(real)))
    assert Path(target.path).exists()


# --- history ----------------------------------------------------------------------------


def test_history_stores_and_returns_scans(tmp_path: Path, result: ScanResult) -> None:
    history = History(tmp_path / "nested" / "history.db")
    assert history.latest_scan() is None
    assert history.list_scans() == []
    history.save_scan(result)
    assert history.get_scan(result.id) == result
    assert history.latest_scan() == result
    assert history.get_scan("missing") is None
    summary = history.list_scans()[0]
    assert summary.id == result.id
    assert summary.root == result.root
    assert summary.recursive is True
    assert summary.artifact_count == result.totals.artifact_count
    assert summary.logical_bytes == result.totals.logical_bytes


def test_history_finds_the_latest_scan_holding_an_artifact(
    tmp_path: Path, workspace_ro: Workspace
) -> None:
    history = History(tmp_path / "history.db")
    whole = scan(workspace_ro.root)
    history.save_scan(whole)
    part = scan(workspace_ro.root / "rust-app")
    history.save_scan(part)
    rust_target = part.artifacts[0].id
    other = next(a.id for a in whole.artifacts if a.id != rust_target)
    found = history.find_artifact(rust_target)
    assert found is not None and found.id == part.id
    found = history.find_artifact(other)
    assert found is not None and found.id == whole.id
    assert history.find_artifact("000000000000") is None


def test_history_keeps_only_the_newest_entries(tmp_path: Path, workspace_ro: Workspace) -> None:
    history = History(tmp_path / "history.db", max_entries=3)
    ids = []
    for _ in range(5):
        found = scan(workspace_ro.root / "rust-app")
        history.save_scan(found)
        ids.append(found.id)
    kept = [s.id for s in history.list_scans()]
    assert kept == ids[:1:-1]  # newest first, three of them
    assert history.get_scan(ids[0]) is None
    assert history.find_artifact(found.artifacts[0].id) is not None


def test_history_stores_cleanup_audits(tmp_path: Path, workspace: Workspace) -> None:
    history = History(tmp_path / "history.db")
    found = scan(workspace.root)
    target = next(a for a in found.artifacts if a.relative_path == "rust-app/target")
    cancelled = cancel(build_plan(found, [target.id], RULES))
    history.save_cleanup(cancelled)
    plan = build_plan(found, [target.id], RULES)
    done = execute(plan, plan.confirmation_token, RULES)
    history.save_cleanup(done)

    assert history.get_cleanup(done.id) == done
    assert history.get_cleanup("missing") is None
    newest, oldest = history.list_cleanups()
    assert (newest.id, newest.cancelled, newest.deleted_bytes) == (done.id, False, 16307)
    assert (oldest.id, oldest.cancelled, oldest.deleted_bytes) == (cancelled.id, True, 0)
    assert newest.selected_bytes == 16307
    assert newest.item_count == 1

    history.clear()
    assert history.list_cleanups() == []
    assert history.list_scans() == []
