"""The Sift terminal interface.

Everything shown here comes from :class:`~sift.core.service.SiftService`; this module only
arranges it on screen and collects key presses. Scans and cleanups run on worker threads
so the interface stays responsive.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from sift.core import reports
from sift.core.models import Artifact, CleanupPlan, CleanupResult, Confidence, ScanResult
from sift.core.scanner import ScanProgress
from sift.core.service import ScanError, SiftService, UnsafeOperation
from sift.tui.screens import (
    ConfirmScreen,
    DetailScreen,
    HelpScreen,
    PreviewScreen,
    ResultScreen,
)

SORTS = ("size", "name", "confidence", "type")
ALL_PROJECTS = "__all__"

CSS = """
#sidebar { width: 32; border-right: solid $primary-darken-2; }
#sidebar Label { padding: 0 1; text-style: bold; }
#projects { height: 1fr; border: none; }
#main { width: 1fr; }
#filter { margin: 0; border: tall $primary-darken-2; }
#status { padding: 0 1; color: $text-muted; height: 1; }
#artifacts { height: 1fr; }
#message { height: 1fr; content-align: center middle; text-align: center; padding: 2 4; }
#detail { height: 9; padding: 0 1; border-top: solid $primary-darken-2; }
.hidden { display: none; }

ModalScreen { align: center middle; }
.dialog {
    width: 76; max-width: 95%; height: auto; max-height: 90%;
    padding: 1 2; border: thick $primary; background: $surface;
}
.dialog.wide { width: 110; }
.dialog-title { text-style: bold; padding-bottom: 1; }
.dialog-title.danger { color: $error; }
.warning { color: $error; text-style: bold; padding: 1 0; }
.buttons { height: auto; padding-top: 1; }
.buttons Button { margin-right: 2; }
#confirm-prompt { padding-bottom: 1; }
"""


class SiftApp(App[None]):
    TITLE = "Sift"
    CSS = CSS
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("s", "cycle_sort", "Sort"),
        Binding("o", "reverse_sort", "Order", show=False),
        Binding("e", "cycle_ecosystem", "Ecosystem"),
        Binding("c", "toggle_cleanable", "Cleanable only", show=False),
        Binding("space", "toggle_select", "Select"),
        Binding("a", "select_all", "Select all", show=False),
        Binding("n", "select_none", "Select none", show=False),
        Binding("d", "cleanup", "Clean up"),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(
        self, service: SiftService, path: str | os.PathLike[str], *, recursive: bool = False
    ) -> None:
        super().__init__()
        self.service = service
        self.scan_path = Path(path)
        self.recursive = recursive
        self.result: ScanResult | None = None
        self.scan_error: str | None = None
        self.scanning = False
        self.selected: set[str] = set()
        self.sort_key = "size"
        self.sort_reverse = False
        self.ecosystem: str | None = None
        self.project: str | None = None
        self.text_filter = ""
        self.cleanable_only = False
        self.last_cleanup: CleanupResult | None = None
        self._visible: list[Artifact] = []

    # --- layout -------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("Projects")
                yield OptionList(id="projects")
            with Vertical(id="main"):
                yield Input(placeholder="Filter by path, type or ecosystem  ( / )", id="filter")
                yield Static("", id="status")
                yield DataTable(id="artifacts", cursor_type="row", zebra_stripes=True)
                yield Static("", id="message", classes="hidden")
                yield Static("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = str(self.scan_path)
        table = self.query_one("#artifacts", DataTable)
        # Path comes first: it must stay visible however narrow the terminal is.
        table.add_column(" ", key="selected", width=1)
        table.add_column("Path", key="path")
        table.add_column("Size", key="size")
        table.add_column("Status", key="status")
        table.add_column("Type", key="type")
        table.add_column("Confidence", key="confidence")
        table.add_column("On disk", key="allocated")
        table.add_column("Files", key="files")
        self.action_rescan()

    # --- scanning -----------------------------------------------------------------------

    def action_rescan(self) -> None:
        if self.scanning:
            return
        self.scanning = True
        self.scan_error = None
        self.selected.clear()
        self._show_message(f"Scanning {self.scan_path} ...")
        self._scan()

    @work(thread=True, exclusive=True, group="scan")
    def _scan(self) -> None:
        def progress(event: ScanProgress) -> None:
            if event.phase == "discovery":
                text = (
                    f"Scanning {self.scan_path} ...\n"
                    f"{event.directories_visited:,} directories, {event.artifacts_found} found"
                )
            elif event.phase == "sizing":
                text = (
                    f"Measuring {self.scan_path} ...\n"
                    f"{event.artifacts_measured} of {event.artifacts_found} artifacts"
                )
            else:
                return
            self.call_from_thread(self._show_message, text)

        try:
            options = self.service.scan_options(recursive=self.recursive)
            result = self.service.scan(self.scan_path, options, progress=progress)
        except ScanError as exc:
            self.call_from_thread(self._scan_failed, str(exc))
        except Exception as exc:  # the interface must survive anything a scan throws
            self.call_from_thread(self._scan_failed, f"{type(exc).__name__}: {exc}")
        else:
            self.call_from_thread(self._scan_finished, result)

    def _scan_failed(self, message: str) -> None:
        self.scanning = False
        self.result = None
        self.scan_error = message
        self.query_one("#projects", OptionList).clear_options()
        self._show_message(
            f"Scan failed\n\n{escape(message)}\n\nPress r to try again or q to quit."
        )
        self._update_status()

    def _scan_finished(self, result: ScanResult) -> None:
        self.scanning = False
        self.result = result
        self.selected &= {artifact.id for artifact in result.artifacts}
        self.project = None
        projects = self.query_one("#projects", OptionList)
        projects.clear_options()
        projects.add_option(
            Option(
                f"All projects  {reports.format_bytes(result.totals.logical_bytes)}",
                id=ALL_PROJECTS,
            )
        )
        for index, project in enumerate(result.projects):
            label = project.relative_path if project.relative_path != "." else project.name
            projects.add_option(
                Option(
                    f"{escape(label)}  {reports.format_bytes(project.logical_bytes)}",
                    id=f"project-{index}",
                )
            )
        projects.highlighted = 0
        self.refresh_table()
        self.query_one("#artifacts", DataTable).focus()
        skipped = [issue for issue in result.issues if issue.kind == "nested-repository"]
        if skipped:
            self.notify(
                f"{reports.count(len(skipped), 'nested repository', 'nested repositories')} "
                "skipped. Run with --recursive to include them.",
                timeout=6,
            )

    # --- table --------------------------------------------------------------------------

    def visible_artifacts(self) -> list[Artifact]:
        """Artifacts after the project, ecosystem, text and cleanable filters, sorted."""
        if self.result is None:
            return []
        needle = self.text_filter.strip().lower()
        shown = []
        for artifact in self.result.artifacts:
            if self.project is not None and artifact.project_path != self.project:
                continue
            if self.ecosystem is not None and artifact.ecosystem.value != self.ecosystem:
                continue
            if self.cleanable_only and not artifact.cleanable:
                continue
            if needle and needle not in (
                f"{artifact.relative_path} {artifact.rule} {artifact.ecosystem.value}".lower()
            ):
                continue
            shown.append(artifact)

        def size_of(artifact: Artifact) -> int:
            return artifact.size.logical_bytes if artifact.size is not None else -1

        if self.sort_key == "size":
            shown.sort(key=lambda a: (-size_of(a), a.relative_path))
        elif self.sort_key == "name":
            shown.sort(key=lambda a: a.relative_path.lower())
        elif self.sort_key == "confidence":
            shown.sort(key=lambda a: (-a.confidence.rank, a.ambiguous, -size_of(a)))
        else:
            shown.sort(key=lambda a: (a.rule, -size_of(a)))
        if self.sort_reverse:
            shown.reverse()
        return shown

    def _row(self, artifact: Artifact) -> list[Text | str]:
        confidence = Text(
            artifact.confidence.value,
            style={
                Confidence.HIGH: "green",
                Confidence.MEDIUM: "yellow",
                Confidence.LOW: "red",
            }[artifact.confidence],
        )
        if artifact.ambiguous:
            confidence.append(" ?", style="bold red")
        status = Text(
            reports.status_label(artifact), style="green" if artifact.cleanable else "yellow"
        )
        return [
            Text("x", style="bold green") if artifact.id in self.selected else "",
            artifact.relative_path,
            Text(reports.describe_size(artifact.size), justify="right"),
            status,
            artifact.rule,
            confidence,
            Text(reports.describe_allocated(artifact.size), justify="right"),
            Text(f"{artifact.size.file_count:,}" if artifact.size else "-", justify="right"),
        ]

    def refresh_table(self) -> None:
        table = self.query_one("#artifacts", DataTable)
        current = self.highlighted_artifact()
        self._visible = self.visible_artifacts()
        table.clear()
        for artifact in self._visible:
            table.add_row(*self._row(artifact), key=artifact.id)
        if self.result is None:
            pass
        elif not self.result.artifacts:
            self._show_message(
                f"No development artifacts found in\n{escape(self.result.root)}\n\n"
                "Press r to scan again or q to quit."
            )
        elif not self._visible:
            self._show_message(
                "No artifacts match the current filters.\n\nPress Esc to clear them."
            )
        else:
            self._show_table()
            index = 0
            if current is not None:
                ids = [artifact.id for artifact in self._visible]
                index = ids.index(current.id) if current.id in ids else 0
            table.move_cursor(row=index)
        self._update_status()
        self._update_detail()

    def highlighted_artifact(self) -> Artifact | None:
        table = self.query_one("#artifacts", DataTable)
        row = table.cursor_row
        if 0 <= row < len(self._visible) and table.row_count:
            return self._visible[row]
        return None

    def _show_message(self, text: str) -> None:
        self.query_one("#artifacts", DataTable).add_class("hidden")
        message = self.query_one("#message", Static)
        message.update(text)
        message.remove_class("hidden")

    def _show_table(self) -> None:
        self.query_one("#message", Static).add_class("hidden")
        self.query_one("#artifacts", DataTable).remove_class("hidden")

    def _update_status(self) -> None:
        status = self.query_one("#status", Static)
        if self.result is None:
            status.update("")
            return
        order = "ascending" if self.sort_reverse else "descending"
        if self.sort_key in ("name", "type"):
            order = "Z-A" if self.sort_reverse else "A-Z"
        chosen = [a for a in self.result.artifacts if a.id in self.selected]
        selected_bytes = sum(a.size.logical_bytes for a in chosen if a.size is not None)
        parts = [
            f"{len(self._visible)} of {reports.count(len(self.result.artifacts), 'artifact')}",
            f"sort: {self.sort_key} ({order})",
            f"ecosystem: {self.ecosystem or 'all'}",
        ]
        if self.cleanable_only:
            parts.append("cleanable only")
        parts.append(f"selected: {len(chosen)} ({reports.format_bytes(selected_bytes)})")
        status.update("  |  ".join(parts))

    def _update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        artifact = self.highlighted_artifact()
        if artifact is None or self.query_one("#artifacts", DataTable).has_class("hidden"):
            detail.update("")
            return
        text = Text()
        text.append(f"{artifact.path}\n", style="bold")
        text.append(f"{artifact.description}\n")
        text.append(f"{artifact.rule} | confidence {artifact.confidence.value}")
        text.append(f" | {reports.status_label(artifact)}\n")
        size = artifact.size
        if size is None:
            text.append("not measured\n", style="yellow")
        else:
            text.append(
                f"{reports.describe_size(size)} in files, "
                f"{reports.describe_allocated(size)} on disk, "
                f"{reports.count(size.file_count, 'file')}\n"
            )
        if artifact.review_reason:
            text.append(f"Cannot be cleaned: {artifact.review_reason}\n", style="yellow")
        evidence = "; ".join(item.detail for item in artifact.evidence)
        text.append(f"Evidence: {evidence}", style="dim")
        detail.update(text)

    # --- events -------------------------------------------------------------------------

    def _ui_ready(self) -> bool:
        """Whether the main widgets are on screen.

        Widget messages can still be delivered while the app is shutting down, after the
        widgets they refer to have been removed. Handlers check this first and do nothing.
        """
        return bool(self.query("#detail")) and bool(self.query("#artifacts"))

    @on(DataTable.RowHighlighted, "#artifacts")
    def _row_highlighted(self) -> None:
        if self._ui_ready():
            self._update_detail()

    @on(DataTable.RowSelected, "#artifacts")
    def _row_selected(self) -> None:
        artifact = self.highlighted_artifact()
        if artifact is not None and self.result is not None:
            self._inspect(artifact.id, self.result.id)

    @on(OptionList.OptionHighlighted, "#projects")
    def _project_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self.result is None or event.option.id is None or not self._ui_ready():
            return
        if event.option.id == ALL_PROJECTS:
            chosen = None
        else:
            chosen = self.result.projects[int(event.option.id.removeprefix("project-"))].path
        if chosen != self.project:
            self.project = chosen
            self.refresh_table()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.text_filter = event.value
        if self._ui_ready():
            self.refresh_table()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        if self._ui_ready():
            self.query_one("#artifacts", DataTable).focus()

    # --- actions ------------------------------------------------------------------------

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_clear_filter(self) -> None:
        self.query_one("#filter", Input).value = ""
        self.ecosystem = None
        self.cleanable_only = False
        self.text_filter = ""
        self.refresh_table()
        self.query_one("#artifacts", DataTable).focus()

    def action_cycle_sort(self) -> None:
        self.sort_key = SORTS[(SORTS.index(self.sort_key) + 1) % len(SORTS)]
        self.sort_reverse = False
        self.refresh_table()

    def action_reverse_sort(self) -> None:
        self.sort_reverse = not self.sort_reverse
        self.refresh_table()

    def action_cycle_ecosystem(self) -> None:
        if self.result is None:
            return
        present = sorted({artifact.ecosystem.value for artifact in self.result.artifacts})
        choices: list[str | None] = [None, *present]
        current = choices.index(self.ecosystem) if self.ecosystem in choices else 0
        self.ecosystem = choices[(current + 1) % len(choices)]
        self.refresh_table()

    def action_toggle_cleanable(self) -> None:
        self.cleanable_only = not self.cleanable_only
        self.refresh_table()

    def _redraw_selection(self, artifacts: list[Artifact]) -> None:
        table = self.query_one("#artifacts", DataTable)
        for artifact in artifacts:
            mark = Text("x", style="bold green") if artifact.id in self.selected else ""
            table.update_cell(artifact.id, "selected", mark)
        self._update_status()

    def action_toggle_select(self) -> None:
        artifact = self.highlighted_artifact()
        if artifact is None:
            return
        if not artifact.cleanable:
            self.notify(
                f"{artifact.relative_path} cannot be cleaned: "
                f"{artifact.review_reason or 'it needs review'}",
                severity="warning",
            )
            return
        self.selected ^= {artifact.id}
        self._redraw_selection([artifact])

    def action_select_all(self) -> None:
        cleanable = [artifact for artifact in self._visible if artifact.cleanable]
        self.selected |= {artifact.id for artifact in cleanable}
        self._redraw_selection(cleanable)

    def action_select_none(self) -> None:
        self.selected.clear()
        self._redraw_selection(self._visible)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    # --- inspect ------------------------------------------------------------------------

    @work(thread=True, group="inspect")
    def _inspect(self, artifact_id: str, scan_id: str) -> None:
        try:
            detail = self.service.inspect(artifact_id, scan_id)
        except Exception as exc:
            self.call_from_thread(self.notify, f"Could not inspect: {exc}", severity="error")
            return
        self.call_from_thread(self.push_screen, DetailScreen(detail))

    # --- cleanup: selection -> preview -> typed confirmation -> result --------------------

    def action_cleanup(self) -> None:
        if self.result is None:
            return
        chosen = [artifact.id for artifact in self.result.artifacts if artifact.id in self.selected]
        if not chosen:
            self.notify("Nothing selected. Press Space on an artifact to select it.")
            return
        self._plan(self.result, chosen, dry_run=False)

    @work(thread=True, group="cleanup")
    def _plan(self, scan: ScanResult, artifact_ids: list[str], dry_run: bool) -> None:
        try:
            plan = self.service.plan_cleanup(scan, artifact_ids, dry_run=dry_run)
        except Exception as exc:
            self.call_from_thread(
                self.notify, f"Could not build a preview: {exc}", severity="error"
            )
            return
        if dry_run:
            self.call_from_thread(self._show_result, self.service.execute_cleanup(plan))
        else:
            self.call_from_thread(self._preview, plan)

    def _preview(self, plan: CleanupPlan) -> None:
        def decided(choice: str | None) -> None:
            if choice == "continue" and plan.deletable:
                self._confirm(plan)
                return
            self._cancelled(plan)
            if choice == "dry-run" and self.result is not None:
                ids = [item.artifact_id for item in plan.items]
                self._plan(self.result, ids, dry_run=True)

        self.push_screen(PreviewScreen(plan), decided)

    def _confirm(self, plan: CleanupPlan) -> None:
        def decided(confirmed: bool | None) -> None:
            if confirmed:
                self._execute(plan)
            else:
                self._cancelled(plan)
                self.notify("Cancelled - nothing was deleted.")

        self.push_screen(ConfirmScreen(plan), decided)

    def _cancelled(self, plan: CleanupPlan) -> None:
        self.last_cleanup = self.service.cancel_cleanup(plan)

    @work(thread=True, group="cleanup")
    def _execute(self, plan: CleanupPlan) -> None:
        try:
            result = self.service.execute_cleanup(plan, plan.confirmation_token)
        except UnsafeOperation as exc:
            self.call_from_thread(
                self.notify, f"Cleanup refused: {exc.reason}", severity="error", timeout=10
            )
            return
        self.call_from_thread(self._show_result, result)

    def _show_result(self, result: CleanupResult) -> None:
        self.last_cleanup = result

        def closed(_: None) -> None:
            if not result.dry_run:
                self.action_rescan()

        self.push_screen(ResultScreen(result), closed)
