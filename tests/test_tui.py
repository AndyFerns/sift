"""The terminal interface, driven headlessly with Textual's Pilot."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Input, OptionList, Static

from conftest import tree_snapshot
from fixture_workspace import Workspace
from sift.core import reports
from sift.core.models import CleanupStatus, ScanOptions
from sift.core.scanner import scan
from sift.core.service import SiftService
from sift.tui.app import SiftApp
from sift.tui.screens import (
    ConfirmScreen,
    DetailScreen,
    HelpScreen,
    PreviewScreen,
    ResultScreen,
    plan_listing,
)

SIZE = (150, 45)
COLUMNS = ("selected", "path", "size", "status", "type", "confidence", "allocated", "files")


async def settle(app: SiftApp, pilot: Pilot[None]) -> None:
    """Wait for background scans and cleanups, then let the screen catch up."""
    for _ in range(3):
        await app.workers.wait_for_complete()
        await pilot.pause()


def table_rows(app: SiftApp) -> list[dict[str, str]]:
    table = app.query_one("#artifacts", DataTable)
    return [
        dict(zip(COLUMNS, (str(cell) for cell in table.get_row_at(i)), strict=True))
        for i in range(table.row_count)
    ]


def text_of(app: SiftApp, selector: str) -> str:
    return str(app.query_one(selector, Static).render())


def plain(renderable: object) -> str:
    console = Console(width=200, record=True, color_system=None, file=io.StringIO())
    console.print(renderable)
    return console.export_text()


def move_to(app: SiftApp, relative_path: str) -> None:
    paths = [row["path"] for row in table_rows(app)]
    app.query_one("#artifacts", DataTable).move_cursor(row=paths.index(relative_path))


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small tree for tests that delete: two real artifacts and one ambiguous directory."""
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
    (node / "node_modules" / "dep" / "index.js").write_bytes(b"x" * 3000)
    (base / "misc" / "build").mkdir(parents=True)
    (base / "misc" / "build" / "important.txt").write_text("hand written")
    return base


# --- scanning and browsing --------------------------------------------------------------


async def test_shows_the_same_artifacts_as_the_core_scanner(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root, recursive=True)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        core = scan(workspace_ro.root, ScanOptions(recursive=True))
        rows = {row["path"]: row for row in table_rows(app)}
        assert list(rows) == [a.relative_path for a in core.artifacts]  # same order: largest first
        for artifact in core.artifacts:
            row = rows[artifact.relative_path]
            assert row["type"] == artifact.rule
            assert row["size"] == reports.describe_size(artifact.size)
            assert row["allocated"] == reports.describe_allocated(artifact.size)
            assert artifact.size is not None
            assert row["files"] == f"{artifact.size.file_count:,}"
            assert row["confidence"] == artifact.confidence.value + (
                " ?" if artifact.ambiguous else ""
            )
            assert row["status"] == reports.status_label(artifact)
            assert row["selected"] == ""
        assert app.result is not None
        assert [a.id for a in app.result.artifacts] == [a.id for a in core.artifacts]
        assert f"{len(core.artifacts)} of {len(core.artifacts)} artifacts" in text_of(
            app, "#status"
        )
        assert app.query_one("#message", Static).has_class("hidden")


async def test_fixture_expectations_hold_in_the_tui(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        rows = {row["path"]: row for row in table_rows(app)}
        expected = workspace_ro.expect(recursive=False)
        assert set(rows) == set(expected)
        for relative, want in expected.items():
            assert rows[relative]["type"] == want.rule
            assert rows[relative]["size"] == reports.format_bytes(want.logical_bytes)
            assert rows[relative]["confidence"].startswith(want.confidence)
            assert rows[relative]["confidence"].endswith("?") is want.ambiguous


async def test_detail_pane_follows_the_cursor(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        move_to(app, "rust-app/target")
        await pilot.pause()
        detail = text_of(app, "#detail")
        assert str(workspace_ro.root.resolve() / "rust-app" / "target") in detail
        assert "Cargo build output" in detail
        assert "Cargo.toml is next to it" in detail
        assert "15.9 KiB in files" in detail

        move_to(app, "ambiguous/build")
        await pilot.pause()
        detail = text_of(app, "#detail")
        assert "Cannot be cleaned" in detail
        assert "only the name matches" in detail

        await pilot.press("down")
        assert str(app.highlighted_artifact().path) in text_of(app, "#detail")  # type: ignore[union-attr]


async def test_project_list_filters_the_table(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.result is not None
        projects = app.query_one("#projects", OptionList)
        assert projects.option_count == len(app.result.projects) + 1
        everything = len(table_rows(app))

        index = next(i for i, p in enumerate(app.result.projects) if p.name == "py-app")
        projects.highlighted = index + 1
        await pilot.pause()
        paths = [row["path"] for row in table_rows(app)]
        assert paths and all(p.startswith("py-app/") for p in paths)
        assert len(paths) == sum(
            1 for e in workspace_ro.expect(recursive=False).values() if e.project == "py-app"
        )

        projects.highlighted = 0
        await pilot.pause()
        assert len(table_rows(app)) == everything


async def test_keyboard_reaches_the_project_list(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.focused is app.query_one("#artifacts", DataTable)
        await pilot.press("shift+tab", "shift+tab")  # table -> filter -> projects
        assert app.focused is app.query_one("#projects", OptionList)
        await pilot.press("down")
        await pilot.pause()
        assert app.project is not None
        assert all(a.project_path == app.project for a in app.visible_artifacts())


async def test_sorting(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        by_size = [row["path"] for row in table_rows(app)]
        assert by_size[0] == "big/node_modules"

        await pilot.press("s")
        by_name = [row["path"] for row in table_rows(app)]
        assert by_name == sorted(by_name, key=str.lower)
        assert "sort: name (A-Z)" in text_of(app, "#status")

        await pilot.press("o")
        assert [row["path"] for row in table_rows(app)] == by_name[::-1]
        assert "sort: name (Z-A)" in text_of(app, "#status")

        await pilot.press("s")
        confidences = [row["confidence"].split()[0] for row in table_rows(app)]
        rank = {"high": 2, "medium": 1, "low": 0}
        assert [rank[c] for c in confidences] == sorted(
            (rank[c] for c in confidences), reverse=True
        )

        await pilot.press("s")
        types = [row["type"] for row in table_rows(app)]
        assert types == sorted(types)

        await pilot.press("s")
        assert [row["path"] for row in table_rows(app)] == by_size


async def test_text_filter(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        total = len(table_rows(app))
        await pilot.press("slash")
        assert app.focused is app.query_one("#filter", Input)
        await pilot.press(*"target")
        await pilot.pause()
        paths = [row["path"] for row in table_rows(app)]
        assert paths and all("target" in p for p in paths)
        assert f"{len(paths)} of {total} artifacts" in text_of(app, "#status")

        await pilot.press("enter")  # back to the table, filter still applied
        assert app.focused is app.query_one("#artifacts", DataTable)
        assert len(table_rows(app)) == len(paths)

        await pilot.press("escape")
        assert len(table_rows(app)) == total
        assert app.query_one("#filter", Input).value == ""


async def test_filter_matches_type_and_ecosystem(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.query_one("#filter", Input).value = "PYTHON.VENV"
        await pilot.pause()
        assert {row["path"] for row in table_rows(app)} == {"py-app/.venv", "py-app/venv"}


async def test_filter_with_no_matches_shows_a_message(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.query_one("#filter", Input).value = "zzz-no-such-thing"
        await pilot.pause()
        assert table_rows(app) == []
        assert "No artifacts match the current filters." in text_of(app, "#message")
        assert app.query_one("#artifacts", DataTable).has_class("hidden")
        assert text_of(app, "#detail") == ""
        await pilot.press("escape")
        assert table_rows(app)
        assert app.query_one("#message", Static).has_class("hidden")


async def test_ecosystem_and_cleanable_filters(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.result is not None
        total = len(app.result.artifacts)
        present = sorted({a.ecosystem.value for a in app.result.artifacts})
        for ecosystem in present:
            await pilot.press("e")
            assert app.ecosystem == ecosystem
            shown = app.visible_artifacts()
            assert shown and all(a.ecosystem.value == ecosystem for a in shown)
            assert f"ecosystem: {ecosystem}" in text_of(app, "#status")
        await pilot.press("e")
        assert app.ecosystem is None
        assert len(table_rows(app)) == total

        await pilot.press("c")
        statuses = {row["status"] for row in table_rows(app)}
        assert statuses == {"cleanable"}
        assert "cleanable only" in text_of(app, "#status")
        await pilot.press("c")
        assert len(table_rows(app)) == total


async def test_inspect_opens_details(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        move_to(app, "rust-app/target")
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, DetailScreen)
        detail = app.screen.detail
        assert detail.artifact.relative_path == "rust-app/target"
        assert detail.exists and detail.cleanup_allowed
        assert detail.children[0].name == "debug"
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DetailScreen)


async def test_help_screen(workspace_ro: Workspace) -> None:
    app = SiftApp(SiftService(), workspace_ro.root / "rust-app")
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, HelpScreen)


# --- empty and error states -------------------------------------------------------------


async def test_empty_directory_state(tmp_path: Path) -> None:
    app = SiftApp(SiftService(), tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert "No development artifacts found" in text_of(app, "#message")
        assert app.query_one("#artifacts", DataTable).has_class("hidden")
        await pilot.press("space", "a", "d", "enter", "s", "e")  # nothing here should crash
        await pilot.pause()
        assert app.selected == set()
        assert not isinstance(app.screen, PreviewScreen)


async def test_scan_error_state_and_retry(tmp_path: Path) -> None:
    target = tmp_path / "later"
    app = SiftApp(SiftService(), target)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert app.result is None
        assert app.scan_error and "does not exist" in app.scan_error
        message = text_of(app, "#message")
        assert "Scan failed" in message and "Press r to try again" in message

        (target / "node_modules" / "dep").mkdir(parents=True)
        (target / "package.json").write_text("{}")
        await pilot.press("r")
        await settle(app, pilot)
        assert app.scan_error is None
        assert [row["path"] for row in table_rows(app)] == ["node_modules"]


async def test_rescan_picks_up_changes(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        assert len(table_rows(app)) == 3
        (project / "node" / ".next").mkdir()
        await pilot.press("r")
        await settle(app, pilot)
        assert "node/.next" in [row["path"] for row in table_rows(app)]


# --- selection --------------------------------------------------------------------------


async def test_space_selects_and_unselects(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        move_to(app, "rust/target")
        await pilot.press("space")
        assert app.result is not None
        target = next(a for a in app.result.artifacts if a.relative_path == "rust/target")
        assert app.selected == {target.id}
        assert {r["path"]: r["selected"] for r in table_rows(app)}["rust/target"] == "x"
        assert "selected: 1 (4.9 KiB)" in text_of(app, "#status")
        await pilot.press("space")
        assert app.selected == set()
        assert {r["path"]: r["selected"] for r in table_rows(app)}["rust/target"] == ""


async def test_artifacts_needing_review_cannot_be_selected(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        move_to(app, "misc/build")
        await pilot.press("space")
        assert app.selected == set()
        await pilot.press("a")
        assert app.result is not None
        assert app.selected == {a.id for a in app.result.artifacts if a.cleanable}
        assert len(app.selected) == 2
        await pilot.press("n")
        assert app.selected == set()
        assert all(row["selected"] == "" for row in table_rows(app))


async def test_select_all_only_takes_what_is_showing(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        app.query_one("#filter", Input).value = "node"
        await pilot.pause()
        app.query_one("#artifacts", DataTable).focus()
        await pilot.press("a")
        assert app.result is not None
        chosen = [a.relative_path for a in app.result.artifacts if a.id in app.selected]
        assert chosen == ["node/node_modules"]


async def test_cleanup_with_nothing_selected_does_nothing(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await pilot.press("d")
        await settle(app, pilot)
        assert not isinstance(app.screen, PreviewScreen)
        assert SiftService().list_cleanups() == []


# --- cleanup: preview, confirmation, backing out ----------------------------------------


async def open_preview(app: SiftApp, pilot: Pilot[None], *paths: str) -> PreviewScreen:
    for path in paths:
        move_to(app, path)
        await pilot.press("space")
    await pilot.press("d")
    await settle(app, pilot)
    assert isinstance(app.screen, PreviewScreen)
    return app.screen


async def test_preview_lists_exactly_what_will_be_deleted(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = tree_snapshot(project)
        screen = await open_preview(app, pilot, "rust/target", "node/node_modules")
        plan = screen.plan
        assert sorted(Path(i.path) for i in plan.deletable) == sorted(
            [project.resolve() / "rust" / "target", project.resolve() / "node" / "node_modules"]
        )
        assert not plan.dry_run and plan.refused == []
        listing = plain(plan_listing(plan))
        assert str(project.resolve() / "rust" / "target") in listing
        assert str(project.resolve() / "node" / "node_modules") in listing
        assert "rust.target" in listing and "node.node_modules" in listing
        assert "to delete 7.9 KiB in 2 artifacts, 3 files" in listing
        assert isinstance(screen.focused, Button) and screen.focused.id == "cancel"
        assert tree_snapshot(project) == before  # opening the preview deletes nothing


@pytest.mark.parametrize("key", ["escape", "enter"])
async def test_backing_out_of_the_preview(project: Path, key: str) -> None:
    """Escape cancels, and so does Enter because Cancel is the focused button."""
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = tree_snapshot(project)
        await open_preview(app, pilot, "rust/target")
        await pilot.press(key)
        await settle(app, pilot)
        assert not isinstance(app.screen, PreviewScreen | ConfirmScreen | ResultScreen)
        assert tree_snapshot(project) == before
        assert app.last_cleanup is not None and app.last_cleanup.cancelled
        audit = SiftService().list_cleanups()
        assert [(a.cancelled, a.deleted_bytes) for a in audit] == [(True, 0)]


@pytest.mark.parametrize("keys", [["escape"], ["enter"], ["tab", "enter"]])
async def test_backing_out_at_the_final_confirmation(project: Path, keys: list[str]) -> None:
    """Escape, Enter on the focused Cancel button, and Enter in the empty box all back out."""
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = tree_snapshot(project)
        await open_preview(app, pilot, "rust/target", "node/node_modules")
        await pilot.click("#continue")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, ConfirmScreen)
        assert isinstance(screen.focused, Button) and screen.focused.id == "cancel"
        assert screen.query_one("#delete", Button).disabled
        listing = plain(plan_listing(screen.plan))
        assert str(project.resolve() / "rust" / "target") in listing

        await pilot.press(*keys)
        await settle(app, pilot)
        if keys == ["tab", "enter"]:  # Enter in the empty confirmation box does nothing at all
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("escape")
            await settle(app, pilot)
        assert not isinstance(app.screen, ConfirmScreen | ResultScreen)
        assert tree_snapshot(project) == before
        assert [(a.cancelled, a.deleted_bytes) for a in SiftService().list_cleanups()] == [
            (True, 0)
        ]


@pytest.mark.parametrize("typed", ["yes", "y", "delet", "delete me", "remove"])
async def test_wrong_confirmation_word_never_deletes(project: Path, typed: str) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = tree_snapshot(project)
        await open_preview(app, pilot, "rust/target")
        await pilot.click("#continue")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, ConfirmScreen)
        screen.query_one("#confirm-input", Input).focus()
        await pilot.press(*typed, "enter")
        await settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)  # still waiting; nothing happened
        assert screen.query_one("#delete", Button).disabled
        await pilot.click("#delete")  # a disabled button cannot be pressed
        await settle(app, pilot)
        assert tree_snapshot(project) == before
        await pilot.click("#cancel")
        await settle(app, pilot)
        assert tree_snapshot(project) == before
        assert not isinstance(app.screen, ConfirmScreen)


async def test_typed_confirmation_deletes_the_selection_only(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_preview(app, pilot, "rust/target")
        await pilot.click("#continue")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, ConfirmScreen)
        screen.query_one("#confirm-input", Input).focus()
        await pilot.press(*"delete")
        await pilot.pause()
        assert not screen.query_one("#delete", Button).disabled
        assert (project / "rust" / "target").exists()  # typing alone deletes nothing
        await pilot.press("enter")
        await settle(app, pilot)

        assert isinstance(app.screen, ResultScreen)
        result = app.screen.result
        assert [(i.relative_path, i.status) for i in result.items] == [
            ("rust/target", CleanupStatus.DELETED)
        ]
        assert result.totals.deleted_bytes == 5043
        assert not (project / "rust" / "target").exists()
        assert (project / "rust" / "src" / "main.rs").exists()
        assert (project / "node" / "node_modules" / "dep" / "index.js").exists()
        assert (project / "misc" / "build" / "important.txt").exists()

        await pilot.press("escape")
        await settle(app, pilot)  # closing the result rescans
        assert sorted(row["path"] for row in table_rows(app)) == ["misc/build", "node/node_modules"]
        assert app.selected == set()
        audit = SiftService().list_cleanups()
        assert [(a.cancelled, a.dry_run, a.deleted_bytes) for a in audit] == [(False, False, 5043)]


async def test_delete_button_works_once_the_word_is_typed(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_preview(app, pilot, "node/node_modules")
        await pilot.click("#continue")
        await settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        app.screen.query_one("#confirm-input", Input).value = " DELETE "
        await pilot.pause()
        await pilot.click("#delete")
        await settle(app, pilot)
        assert isinstance(app.screen, ResultScreen)
        assert not (project / "node" / "node_modules").exists()
        assert (project / "node" / "package.json").exists()


async def test_dry_run_from_the_preview(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        before = tree_snapshot(project)
        await open_preview(app, pilot, "rust/target", "node/node_modules")
        await pilot.click("#dry-run")
        await settle(app, pilot)
        assert isinstance(app.screen, ResultScreen)
        result = app.screen.result
        assert result.dry_run
        assert {i.status for i in result.items} == {CleanupStatus.DRY_RUN}
        assert result.totals.would_delete_bytes == 8043
        assert result.totals.deleted_bytes == 0
        assert tree_snapshot(project) == before
        await pilot.press("escape")
        await settle(app, pilot)
        assert len(app.selected) == 2  # the selection survives a dry run


async def test_artifact_that_changed_is_refused_at_execution(project: Path) -> None:
    app = SiftApp(SiftService(), project)
    async with app.run_test(size=SIZE) as pilot:
        await settle(app, pilot)
        await open_preview(app, pilot, "node/node_modules")
        await pilot.click("#continue")
        await settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        (project / "node" / "package.json").unlink()  # the evidence disappears mid-flow
        app.screen.query_one("#confirm-input", Input).value = "delete"
        await pilot.pause()
        await pilot.click("#delete")
        await settle(app, pilot)
        assert isinstance(app.screen, ResultScreen)
        assert [i.status for i in app.screen.result.items] == [CleanupStatus.REFUSED]
        assert (project / "node" / "node_modules" / "dep" / "index.js").exists()
