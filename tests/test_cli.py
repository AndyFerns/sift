"""The CLI, driven through Typer's test runner and as a real subprocess."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import tree_snapshot
from fixture_workspace import Workspace
from sift import __version__
from sift.cli.app import EXIT_ERROR, EXIT_OK, EXIT_UNSAFE, EXIT_USAGE, app, parse_selection
from sift.core import cleanup
from sift.core.config import config_path
from sift.core.models import ScanOptions
from sift.core.scanner import scan
from sift.core.service import SiftService

runner = CliRunner()
WIDE = {"COLUMNS": "220"}


def run(*args: object, input: str | None = None, columns: str = "220"):
    return runner.invoke(app, [str(a) for a in args], input=input, env={"COLUMNS": columns})


def scan_json(*args: object) -> dict:
    result = run("scan", *args, "--format", "json")
    assert result.exit_code == EXIT_OK, result.output
    return json.loads(result.stdout)


def numbers(workspace: Workspace, *relative: str, recursive: bool = False) -> str:
    """The selection string a user would type to pick these artifacts."""
    found = scan(workspace.root, ScanOptions(recursive=recursive))
    order = [a.relative_path for a in found.artifacts]
    return ",".join(str(order.index(r) + 1) for r in relative)


# --- basics -----------------------------------------------------------------------------


def test_version() -> None:
    result = run("--version")
    assert result.exit_code == EXIT_OK
    assert result.stdout.strip() == f"sift {__version__}"


def test_help_lists_every_command() -> None:
    result = run("--help")
    assert result.exit_code == EXIT_OK
    for command in ("scan", "inspect", "clean", "config", "tui", "serve"):
        assert command in result.stdout


def test_no_arguments_shows_help() -> None:
    assert "Usage" in run().output


def test_unknown_option_is_a_usage_error(workspace_ro: Workspace) -> None:
    assert run("scan", workspace_ro.root, "--frobnicate").exit_code == EXIT_USAGE


# --- scan -------------------------------------------------------------------------------


def test_scan_table_lists_every_artifact(workspace_ro: Workspace) -> None:
    result = run("scan", workspace_ro.root)
    assert result.exit_code == EXIT_OK
    expected = workspace_ro.expect(recursive=False)
    for relative, want in expected.items():
        assert relative in result.stdout
        assert want.rule in result.stdout
    assert f"{len(expected)} artifacts" in result.stdout
    assert "cleanable" in result.stdout
    assert "need review" in result.stdout
    assert "nested-repository" in result.stdout


def test_scan_table_fits_a_narrow_terminal(workspace_ro: Workspace) -> None:
    result = run("scan", workspace_ro.root / "py-app", columns="80")
    assert result.exit_code == EXIT_OK
    lines = result.stdout.splitlines()
    assert max(len(line) for line in lines[:12]) <= 80
    assert "python.venv" in result.stdout
    assert ".venv" in result.stdout
    assert "review ?" in result.stdout


def test_scan_json_matches_the_core_scanner(workspace_ro: Workspace) -> None:
    data = scan_json(workspace_ro.root)
    core = scan(workspace_ro.root)
    assert data["root"] == core.root
    assert data["totals"] == json.loads(core.totals.model_dump_json())
    got = {a["id"]: a for a in data["artifacts"]}
    assert list(got) == [a.id for a in core.artifacts]  # same artifacts, same order
    for artifact in core.artifacts:
        row = got[artifact.id]
        assert row["path"] == artifact.path
        assert row["relative_path"] == artifact.relative_path
        assert row["rule"] == artifact.rule
        assert row["ecosystem"] == artifact.ecosystem.value
        assert row["confidence"] == artifact.confidence.value
        assert row["ambiguous"] is artifact.ambiguous
        assert row["cleanable"] is artifact.cleanable
        assert artifact.size is not None
        assert row["size"]["logical_bytes"] == artifact.size.logical_bytes
        assert row["size"]["allocated_bytes"] == artifact.size.allocated_bytes
        assert row["size"]["file_count"] == artifact.size.file_count


def test_scan_json_is_only_json(workspace_ro: Workspace) -> None:
    result = run("scan", workspace_ro.root / "rust-app", "--format", "json")
    assert result.stdout.lstrip().startswith("{")
    assert result.stdout.rstrip().endswith("}")
    json.loads(result.stdout)


def test_scan_csv(workspace_ro: Workspace) -> None:
    result = run("scan", workspace_ro.root, "--format", "csv")
    assert result.exit_code == EXIT_OK
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    expected = workspace_ro.expect(recursive=False)
    assert {r["relative_path"] for r in rows} == set(expected)
    for row in rows:
        want = expected[row["relative_path"]]
        assert row["rule"] == want.rule
        assert row["confidence"] == want.confidence
        assert row["ambiguous"] == str(want.ambiguous).lower()
        assert int(row["logical_bytes"]) == want.logical_bytes
        assert int(row["file_count"]) == want.file_count


def test_scan_recursive(workspace_ro: Workspace) -> None:
    plain = {a["relative_path"] for a in scan_json(workspace_ro.root)["artifacts"]}
    deep = {a["relative_path"] for a in scan_json(workspace_ro.root, "--recursive")["artifacts"]}
    assert deep - plain == {"nested/outer/node_modules", "nested/outer/inner/target"}
    assert deep == set(workspace_ro.expect(recursive=True))
    assert {a["relative_path"] for a in scan_json(workspace_ro.root, "-r")["artifacts"]} == deep


def test_scan_options(workspace_ro: Workspace) -> None:
    shallow = scan_json(workspace_ro.root, "--depth", "1")
    assert "py-app/src/pkg/__pycache__" not in {a["relative_path"] for a in shallow["artifacts"]}
    assert shallow["options"]["max_depth"] == 1

    excluded = scan_json(workspace_ro.root, "--exclude", "node-app/", "-x", "**/target/")
    paths = {a["relative_path"] for a in excluded["artifacts"]}
    assert not [p for p in paths if p.startswith("node-app/") or p.endswith("/target")]

    lenient = scan_json(workspace_ro.root, "--min-confidence", "low")
    by_path = {a["relative_path"]: a for a in lenient["artifacts"]}
    assert by_path["node-app/dist"]["cleanable"] is True
    assert by_path["ambiguous/build"]["cleanable"] is False

    unsized = scan_json(workspace_ro.root, "--no-size")
    assert all(a["size"] is None for a in unsized["artifacts"])

    budget = scan_json(workspace_ro.root, "--time-budget", "60")
    assert all(a["size"] is not None for a in budget["artifacts"])


def test_scan_writes_a_report_file(workspace_ro: Workspace, tmp_path: Path) -> None:
    for fmt, start in (("json", "{"), ("csv", "id,relative_path"), ("table", "ID")):
        target = tmp_path / f"report.{fmt}"
        result = run("scan", workspace_ro.root / "rust-app", "--format", fmt, "--output", target)
        assert result.exit_code == EXIT_OK
        assert result.stdout == ""
        assert target.read_text(encoding="utf-8").lstrip().startswith(start)
        assert "target" in target.read_text(encoding="utf-8")


def test_scan_records_history_unless_told_not_to(workspace_ro: Workspace) -> None:
    run("scan", workspace_ro.root / "rust-app", "--no-history")
    assert SiftService().list_scans() == []
    run("scan", workspace_ro.root / "rust-app")
    assert len(SiftService().list_scans()) == 1


def test_scan_empty_directory(tmp_path: Path) -> None:
    result = run("scan", tmp_path)
    assert result.exit_code == EXIT_OK
    assert "No development artifacts found." in result.stdout


def test_scan_missing_path(tmp_path: Path) -> None:
    result = run("scan", tmp_path / "nope")
    assert result.exit_code == EXIT_USAGE
    assert "does not exist" in result.stderr
    assert result.stdout == ""


def test_scan_path_that_is_a_file(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("x")
    result = run("scan", tmp_path / "file.txt")
    assert result.exit_code == EXIT_USAGE
    assert "not a directory" in result.stderr


def test_invalid_config_is_a_usage_error(workspace_ro: Workspace) -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text("[scan]\nmax_depht = 3\n")
    result = run("scan", workspace_ro.root)
    assert result.exit_code == EXIT_USAGE
    assert "max_depht" in result.stderr


def test_config_file_shapes_the_scan(workspace_ro: Workspace) -> None:
    config_path().parent.mkdir(parents=True)
    config_path().write_text(
        '[scan]\nexclude = ["node-app/"]\n[detectors.go]\nartifact_dirs = ["bin"]\n'
    )
    paths = {a["relative_path"] for a in scan_json(workspace_ro.root)["artifacts"]}
    assert "go-app/bin" in paths
    assert not [p for p in paths if p.startswith("node-app/")]


# --- inspect ----------------------------------------------------------------------------


def test_inspect_after_a_scan(workspace_ro: Workspace) -> None:
    data = scan_json(workspace_ro.root)
    target = next(a for a in data["artifacts"] if a["relative_path"] == "rust-app/target")
    result = run("inspect", target["id"])
    assert result.exit_code == EXIT_OK
    for text in (target["id"], "rust.target", "Cargo.toml", "CACHEDIR.TAG", "15.9 KiB", "debug"):
        assert text in result.stdout
    assert "allowed" in result.stdout


def test_inspect_json(workspace_ro: Workspace) -> None:
    data = scan_json(workspace_ro.root)
    target = next(a for a in data["artifacts"] if a["relative_path"] == "ambiguous/build")
    result = run("inspect", target["id"], "--format", "json")
    detail = json.loads(result.stdout)
    assert detail["artifact"]["id"] == target["id"]
    assert detail["exists"] is True
    assert detail["cleanup_allowed"] is False
    assert "ambiguous" in detail["cleanup_refusal"]
    assert detail["current_size"]["logical_bytes"] == 100


def test_inspect_unknown_id() -> None:
    result = run("inspect", "000000000000")
    assert result.exit_code == EXIT_ERROR
    assert "no artifact with ID 000000000000" in result.stderr
    assert "sift scan" in result.stderr


def test_inspect_with_root_needs_no_history(workspace_ro: Workspace) -> None:
    core = scan(workspace_ro.root, ScanOptions(recursive=True))
    nested = next(a for a in core.artifacts if a.relative_path == "nested/outer/inner/target")
    assert run("inspect", nested.id).exit_code == EXIT_ERROR  # nothing scanned yet
    result = run("inspect", nested.id, "--root", workspace_ro.root)
    assert result.exit_code == EXIT_OK
    assert "rust.target" in result.stdout
    assert SiftService().list_scans() == []  # --root does not write history
    assert run("inspect", "000000000000", "--root", workspace_ro.root).exit_code == EXIT_ERROR


# --- clean: never destructive by default ------------------------------------------------


@pytest.mark.parametrize("flags", [[], ["--dry-run"], ["-n"]])
def test_clean_without_interactive_is_a_dry_run(workspace: Workspace, flags: list[str]) -> None:
    before = tree_snapshot(workspace.root)
    result = run("clean", workspace.root, *flags)
    assert result.exit_code == EXIT_OK
    assert tree_snapshot(workspace.root) == before
    assert "Would delete" in result.stdout
    assert "Dry run - nothing was deleted." in result.stdout
    assert "would delete" in result.stdout
    assert str(workspace.root.resolve() / "rust-app" / "target") in result.stdout.replace("\n", "")
    assert ("No cleanup mode given" in result.stderr) is (not flags)


def test_dry_run_previews_only_cleanable_artifacts(workspace: Workspace) -> None:
    result = run("clean", workspace.root, "--dry-run")
    expected = workspace.expect(recursive=False)
    cleanable = sum(1 for e in expected.values() if e.cleanable)
    assert f"Would delete {cleanable} artifact(s)" in result.stdout
    assert "Refused" not in result.stdout


def test_clean_reports_when_there_is_nothing(tmp_path: Path) -> None:
    result = run("clean", tmp_path, "--interactive", input="")
    assert result.exit_code == EXIT_OK
    assert "No development artifacts found." in result.stdout


# --- clean: selection, preview, confirmation --------------------------------------------


def test_enter_at_the_selection_prompt_cancels(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    result = run("clean", workspace.root, "--interactive", input="\n")
    assert result.exit_code == EXIT_OK
    assert "Cancelled - nothing was deleted." in result.stdout
    assert tree_snapshot(workspace.root) == before
    assert SiftService().list_cleanups() == []  # no preview was built


def test_end_of_input_at_the_selection_prompt_cancels(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    result = run("clean", workspace.root, "-i", input="")
    assert result.exit_code == EXIT_OK
    assert "Cancelled - nothing was deleted." in result.stdout
    assert tree_snapshot(workspace.root) == before


def test_preview_lists_exactly_what_will_be_deleted(workspace: Workspace) -> None:
    pick = numbers(workspace, "rust-app/target", "node-app/.next")
    result = run("clean", workspace.root, "-i", input=f"{pick}\n\n")
    flat = result.stdout.replace("\n", "")
    assert "Will delete 2 artifact(s)" in result.stdout
    assert str(workspace.root.resolve() / "rust-app" / "target") in flat
    assert str(workspace.root.resolve() / "node-app" / ".next") in flat
    assert "rust.target" in result.stdout and "node.next" in result.stdout
    assert "2 artifacts, 7 files" in result.stdout
    assert "23.9 KiB" in result.stdout  # 16307 + 8213 bytes
    assert "Deletion is permanent" in result.stderr
    assert 'Type "delete"' in result.stderr
    assert "press Enter to cancel" in result.stderr


@pytest.mark.parametrize(
    "answer", ["\n", "n\n", "no\n", "y\n", "yes\n", "delet\n", "delete!\n", ""]
)
def test_anything_but_the_confirmation_word_backs_out(workspace: Workspace, answer: str) -> None:
    before = tree_snapshot(workspace.root)
    pick = numbers(workspace, "rust-app/target", "node-app/node_modules")
    result = run("clean", workspace.root, "--interactive", input=f"{pick}\n{answer}")
    assert result.exit_code == EXIT_OK
    assert "Cancelled - nothing was deleted." in result.stdout
    assert tree_snapshot(workspace.root) == before
    audit = SiftService().list_cleanups()
    assert [(a.cancelled, a.deleted_bytes, a.item_count) for a in audit] == [(True, 0, 2)]


def test_typing_delete_deletes_the_selection_and_nothing_else(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    pick = numbers(workspace, "rust-app/target", "node-app/.next")
    result = run("clean", workspace.root, "-i", input=f"{pick}\ndelete\n")
    assert result.exit_code == EXIT_OK, result.output
    assert not (workspace.root / "rust-app" / "target").exists()
    assert not (workspace.root / "node-app" / ".next").exists()
    after = tree_snapshot(workspace.root)
    gone = set(before) - set(after)
    assert all(Path(p).parts[:2] in {("rust-app", "target"), ("node-app", ".next")} for p in gone)
    assert (workspace.root / "rust-app" / "src" / "main.rs").exists()
    assert (workspace.root / "node-app" / "node_modules").exists()
    for label in ("detected", "selected", "deleted", "failed", "refused"):
        assert label in result.stdout
    assert "deleted          23.9 KiB   2 artifacts" in result.stdout
    assert "deleting rust-app/target" in result.stderr
    audit = SiftService().list_cleanups()
    assert [(a.cancelled, a.dry_run, a.deleted_bytes) for a in audit] == [(False, False, 24520)]


def test_confirmation_word_is_case_insensitive(workspace: Workspace) -> None:
    pick = numbers(workspace, "java-maven/target")
    result = run("clean", workspace.root, "-i", input=f"{pick}\n  DELETE \n")
    assert result.exit_code == EXIT_OK
    assert not (workspace.root / "java-maven" / "target").exists()


def test_selecting_all_takes_only_cleanable_artifacts(workspace: Workspace) -> None:
    result = run("clean", workspace.root, "-i", input="all\ndelete\n")
    assert result.exit_code == EXIT_OK, result.output
    expected = workspace.expect(recursive=False)
    for relative, want in expected.items():
        assert os.path.lexists(workspace.root / relative) is (not want.cleanable), relative
    assert "Refused" not in result.stdout


def test_ranges_and_lists_select_the_numbered_rows(workspace: Workspace) -> None:
    order = [a.relative_path for a in scan(workspace.root).artifacts]
    result = run("clean", workspace.root, "-i", "--dry-run", input="1-2, 4\n")
    assert result.exit_code == EXIT_OK
    flat = result.stdout.replace("\n", "")
    for index in (0, 1, 3):
        assert str(workspace.root.resolve() / order[index]) in flat
    assert "Dry run - nothing was deleted." in result.stdout
    assert os.path.lexists(workspace.root / order[0])


def test_bad_selection_is_asked_again_then_cancelled(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    result = run("clean", workspace.root, "-i", input="banana\n999\n5-2\n")
    assert result.exit_code == EXIT_OK
    assert result.stderr.count("Not understood") == 3
    assert "Cancelled - nothing was deleted." in result.stdout
    assert tree_snapshot(workspace.root) == before

    recovered = run("clean", workspace.root / "rust-app", "-i", input="oops\n1\ndelete\n")
    assert recovered.exit_code == EXIT_OK
    assert not (workspace.root / "rust-app" / "target").exists()


def test_refused_selection_is_shown_and_never_deleted(workspace: Workspace) -> None:
    pick = numbers(workspace, "rust-app/target", "ambiguous/build", "node-app/dist")
    result = run("clean", workspace.root, "-i", input=f"{pick}\ndelete\n")
    assert result.exit_code == EXIT_UNSAFE
    assert "Refused: 2 selected artifact(s) will NOT be deleted" in result.stdout
    assert "ambiguous" in result.stdout
    assert "threshold" in result.stdout
    assert not (workspace.root / "rust-app" / "target").exists()
    assert (workspace.root / "ambiguous" / "build" / "out.txt").exists()
    assert (workspace.root / "node-app" / "dist" / "bundle.js").exists()


def test_selection_with_nothing_deletable_stops_before_asking(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    pick = numbers(workspace, "ambiguous/build", "py-app/venv")
    result = run("clean", workspace.root, "-i", input=f"{pick}\ndelete\n")
    assert result.exit_code == EXIT_UNSAFE
    assert "every selected artifact was refused" in result.stdout
    assert 'Type "delete"' not in result.stderr
    assert tree_snapshot(workspace.root) == before


def test_interactive_dry_run_asks_for_no_confirmation(workspace: Workspace) -> None:
    before = tree_snapshot(workspace.root)
    pick = numbers(workspace, "rust-app/target")
    result = run("clean", workspace.root, "-i", "--dry-run", input=f"{pick}\n")
    assert result.exit_code == EXIT_OK
    assert "Would delete 1 artifact(s)" in result.stdout
    assert 'Type "delete"' not in result.stderr
    assert tree_snapshot(workspace.root) == before
    assert SiftService().list_cleanups()[0].dry_run


def test_failed_deletion_exits_with_an_error(
    workspace: Workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cleanup, "_remove", lambda path, kind: ["simulated failure"])
    pick = numbers(workspace, "rust-app/target")
    result = run("clean", workspace.root, "-i", input=f"{pick}\ndelete\n")
    assert result.exit_code == EXIT_ERROR
    assert "failed" in result.stdout
    assert "simulated failure" in result.stdout
    assert (workspace.root / "rust-app" / "target").exists()


def test_clean_recursive_reaches_nested_repositories(workspace: Workspace) -> None:
    pick = numbers(workspace, "nested/outer/inner/target", recursive=True)
    result = run("clean", workspace.root, "-r", "-i", input=f"{pick}\ndelete\n")
    assert result.exit_code == EXIT_OK
    assert not (workspace.root / "nested" / "outer" / "inner" / "target").exists()
    assert (workspace.root / "nested" / "outer" / "inner" / "Cargo.toml").exists()


def test_output_never_calls_deletion_reversible(workspace: Workspace) -> None:
    pick = numbers(workspace, "rust-app/target")
    result = run("clean", workspace.root, "-i", input=f"{pick}\ndelete\n")
    text = (result.stdout + result.stderr).lower()
    assert "permanent" in text
    for word in ("reversible", "undo", "restore it", "recycle", "trash"):
        assert word not in text


# --- selection parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1", [0]),
        ("3,1", [2, 0]),
        ("1 2  3", [0, 1, 2]),
        ("2-4", [1, 2, 3]),
        ("1,3-5,9", [0, 2, 3, 4, 8]),
        ("2,2,1-2", [1, 0]),
        (" ALL ", None),
        ("", []),
    ],
)
def test_parse_selection(text: str, expected: list[int] | None) -> None:
    assert parse_selection(text, 9) == expected


@pytest.mark.parametrize("text", ["0", "10", "a", "1-", "-3", "4-2", "1.5", "1;2", "8-10", "*"])
def test_parse_selection_rejects_nonsense(text: str) -> None:
    with pytest.raises(ValueError):
        parse_selection(text, 9)


# --- config -----------------------------------------------------------------------------


def test_config_show_defaults() -> None:
    result = run("config", "show")
    assert result.exit_code == EXIT_OK
    assert 'min_confidence = "medium"' in result.stdout
    assert "[history]" in result.stdout
    assert "not created yet" in result.stderr
    assert str(config_path()) in result.stderr.replace("\n", "")


def test_config_show_json() -> None:
    data = json.loads(run("config", "show", "--format", "json").stdout)
    assert data["path"] == str(config_path())
    assert data["exists"] is False
    assert data["config"]["scan"]["min_confidence"] == "medium"
    assert data["config"]["server"]["host"] == "127.0.0.1"


def test_config_init_then_show() -> None:
    created = run("config", "init")
    assert created.exit_code == EXIT_OK
    assert config_path().exists()
    assert str(config_path()) in created.stdout.replace("\n", "")
    assert "# Sift configuration." in config_path().read_text()

    again = run("config", "init")
    assert again.exit_code == EXIT_ERROR
    assert "already exists" in again.stderr

    config_path().write_text("[scan]\nmax_depth = 2\n")
    assert "max_depth = 2" in run("config", "show").stdout
    assert run("config", "init", "--force").exit_code == EXIT_OK
    assert "max_depth = 2" not in run("config", "show").stdout
    assert json.loads(run("config", "show", "-f", "json").stdout)["exists"] is True


def test_config_path() -> None:
    assert run("config", "path").stdout.strip() == str(config_path())


# --- the real executable ----------------------------------------------------------------


def launch(*args: object, input: str | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "COLUMNS": "200", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "sift", *[str(a) for a in args]],
        input=input,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
        check=False,
    )


def test_subprocess_scan_matches_the_core_scanner(workspace_ro: Workspace) -> None:
    done = launch("scan", workspace_ro.root, "--recursive", "--format", "json")
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)
    core = scan(workspace_ro.root, ScanOptions(recursive=True))
    assert [
        (a["id"], a["path"], a["rule"], a["size"]["logical_bytes"]) for a in data["artifacts"]
    ] == [(a.id, a.path, a.rule, a.size.logical_bytes) for a in core.artifacts if a.size]
    assert done.stderr == ""  # no progress noise when the output is being piped


def test_subprocess_exit_codes(workspace_ro: Workspace, tmp_path: Path) -> None:
    assert launch("--version").stdout.strip() == f"sift {__version__}"
    assert launch("scan", tmp_path / "nope").returncode == EXIT_USAGE
    assert launch("inspect", "000000000000").returncode == EXIT_ERROR
    assert launch("scan", workspace_ro.root / "rust-app").returncode == EXIT_OK


def test_subprocess_interactive_cleanup(workspace: Workspace) -> None:
    project = workspace.root / "rust-app"
    cancelled = launch("clean", project, "--interactive", input="1\n\n")
    assert cancelled.returncode == EXIT_OK
    assert "Cancelled - nothing was deleted." in cancelled.stdout
    assert (project / "target").exists()

    closed = launch("clean", project, "--interactive", input="1\n")  # stdin ends at the gate
    assert closed.returncode == EXIT_OK
    assert (project / "target").exists()

    confirmed = launch("clean", project, "--interactive", input="1\ndelete\n")
    assert confirmed.returncode == EXIT_OK, confirmed.stderr
    assert not (project / "target").exists()
    assert (project / "Cargo.toml").exists()
    assert "Deletion is permanent" in confirmed.stderr
