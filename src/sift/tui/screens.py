"""Modal screens: help, artifact details, and the cleanup preview / confirm / result flow."""

from __future__ import annotations

from typing import ClassVar

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from sift.core import reports
from sift.core.cleanup import CONFIRMATION_WORD, PERMANENT_NOTICE
from sift.core.models import ArtifactDetail, CleanupPlan, CleanupResult

HELP_TEXT = """\
[b]Moving around[/b]
  Up / Down, PgUp / PgDn     move through artifacts
  Tab / Shift+Tab            switch between projects, filter and artifacts
  Enter                      inspect the highlighted artifact

[b]Finding things[/b]
  /                          filter by path, type or ecosystem
  Esc                        clear the filter
  s                          change the sort column
  o                          reverse the sort order
  e                          cycle the ecosystem filter
  c                          show only cleanable artifacts

[b]Cleaning up[/b]
  Space                      select or unselect the highlighted artifact
  a                          select every cleanable artifact that is showing
  n                          clear the selection
  d                          preview a cleanup of the selection

  Nothing is deleted until you have seen the preview and typed the
  confirmation word. Artifacts marked ? need review and cannot be selected.

[b]Other[/b]
  r                          scan again
  ?                          this help
  q                          quit
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,question_mark", "dismiss", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label("Sift keys", classes="dialog-title")
            yield Static(HELP_TEXT)
            yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed)
    def _close(self) -> None:
        self.dismiss(None)


class DetailScreen(ModalScreen[None]):
    """Everything known about one artifact, re-checked on disk."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,enter", "dismiss", "Close")]

    def __init__(self, detail: ArtifactDetail) -> None:
        super().__init__()
        self.detail = detail

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog wide"):
            yield Label(f"Artifact {self.detail.artifact.id}", classes="dialog-title")
            yield Static(reports.detail_report(self.detail), id="detail-body")
            yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed)
    def _close(self) -> None:
        self.dismiss(None)


def plan_listing(plan: CleanupPlan) -> RenderableType:
    """Exactly what the plan would delete, and what it refuses to."""
    parts: list[RenderableType] = []
    if plan.deletable:
        table = Table(box=None, pad_edge=False, header_style="bold", expand=True)
        table.add_column("Will be deleted", overflow="fold", ratio=1)
        table.add_column("Type", no_wrap=True)
        table.add_column("Conf.", no_wrap=True)
        table.add_column("Files", justify="right", no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        for item in plan.deletable:
            table.add_row(
                escape(item.path),
                item.rule,
                item.confidence.value,
                f"{item.file_count:,}",
                reports.format_bytes(item.logical_bytes),
            )
        parts.append(table)
    else:
        parts.append(Text("Nothing in the selection can be deleted.", style="bold"))
    if plan.refused:
        refused = Table(box=None, pad_edge=False, header_style="bold yellow", expand=True)
        refused.add_column("Refused - will NOT be deleted", overflow="fold", ratio=1)
        refused.add_column("Reason", overflow="fold", ratio=1)
        for item in plan.refused:
            refused.add_row(escape(item.path), escape(item.refusal_reason or "refused"))
        parts.append(Text())
        parts.append(refused)
    totals = plan.totals
    summary = Text()
    summary.append(
        f"\nDetected {reports.format_bytes(totals.detected_bytes)} in "
        f"{reports.count(totals.detected_count, 'artifact')}"
        f"  |  selected {reports.format_bytes(totals.selected_bytes)}"
        f"  |  to delete {reports.format_bytes(totals.deletable_bytes)} in "
        f"{reports.count(totals.deletable_count, 'artifact')}, "
        f"{reports.count(totals.deletable_file_count, 'file')}",
        style="bold",
    )
    parts.append(summary)
    return Group(*parts)


class PreviewScreen(ModalScreen[str]):
    """Step one of a cleanup: see what would happen. Dismisses with cancel/dry-run/continue."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, plan: CleanupPlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog wide"):
            yield Label("Cleanup preview - nothing has been deleted", classes="dialog-title")
            yield Static(plan_listing(self.plan), id="preview-body")
            yield Static(PERMANENT_NOTICE, classes="warning")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel", variant="primary")
                yield Button("Dry run", id="dry-run")
                yield Button(
                    "Continue to confirmation",
                    id="continue",
                    variant="error",
                    disabled=not self.plan.deletable,
                )

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "cancel")


class ConfirmScreen(ModalScreen[bool]):
    """The final gate. Lists what will be deleted once more; only the typed word proceeds."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, plan: CleanupPlan) -> None:
        super().__init__()
        self.plan = plan

    def compose(self) -> ComposeResult:
        totals = self.plan.totals
        with VerticalScroll(classes="dialog wide"):
            yield Label("Confirm permanent deletion", classes="dialog-title danger")
            yield Static(plan_listing(self.plan), id="confirm-body")
            yield Static(PERMANENT_NOTICE, classes="warning")
            yield Label(
                f'Type "{CONFIRMATION_WORD}" to delete '
                f"{reports.count(totals.deletable_count, 'artifact')} "
                f"({reports.format_bytes(totals.deletable_bytes)}). "
                "Esc, Cancel or anything else backs out.",
                id="confirm-prompt",
            )
            yield Input(placeholder=f"type {CONFIRMATION_WORD} to confirm", id="confirm-input")
            with Horizontal(classes="buttons"):
                yield Button("Cancel - delete nothing", id="cancel", variant="primary")
                yield Button("Delete permanently", id="delete", variant="error", disabled=True)

    def on_mount(self) -> None:
        # Cancel has focus, so a stray Enter backs out rather than deleting.
        self.query_one("#cancel", Button).focus()

    def _confirmed(self) -> bool:
        typed = self.query_one("#confirm-input", Input).value
        return typed.strip().lower() == CONFIRMATION_WORD

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Input.Changed, "#confirm-input")
    def _typed(self) -> None:
        self.query_one("#delete", Button).disabled = not self._confirmed()

    @on(Input.Submitted, "#confirm-input")
    def _submitted(self) -> None:
        if self._confirmed():
            self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#delete")
    def _delete(self) -> None:
        if self._confirmed():
            self.dismiss(True)


class ResultScreen(ModalScreen[None]):
    """What a cleanup (or dry run) actually did."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,enter,q", "dismiss", "Close")]

    def __init__(self, result: CleanupResult) -> None:
        super().__init__()
        self.result = result

    def compose(self) -> ComposeResult:
        if self.result.dry_run:
            title = "Dry run - nothing was deleted"
        elif self.result.cancelled:
            title = "Cancelled - nothing was deleted"
        else:
            title = "Cleanup finished"
        with VerticalScroll(classes="dialog wide"):
            yield Label(title, classes="dialog-title")
            yield Static(reports.cleanup_report(self.result), id="result-body")
            yield Button("Close", id="close", variant="primary")

    @on(Button.Pressed)
    def _close(self) -> None:
        self.dismiss(None)
