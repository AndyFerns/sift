"""Reports: JSON, CSV and terminal renderings of scans and cleanups.

The terminal renderers return Rich objects, so the CLI can print them and tests can
render them to plain text. Sizes are always formatted by :func:`format_bytes` so the same
number looks the same in every interface.
"""

from __future__ import annotations

import csv
import io

from rich.console import Group, RenderableType
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from sift.core.models import (
    Artifact,
    ArtifactDetail,
    CleanupPlan,
    CleanupResult,
    CleanupStatus,
    Confidence,
    ScanResult,
    SizeInfo,
    SizeMethod,
)

_UNITS = ("KiB", "MiB", "GiB", "TiB", "PiB")

CSV_COLUMNS = (
    "id",
    "relative_path",
    "path",
    "name",
    "kind",
    "ecosystem",
    "rule",
    "category",
    "confidence",
    "ambiguous",
    "cleanable",
    "review_reason",
    "project_path",
    "measured",
    "complete",
    "logical_bytes",
    "allocated_bytes",
    "allocated_method",
    "file_count",
    "dir_count",
)


def format_bytes(value: int | None) -> str:
    """Human-readable size in binary units, e.g. ``1.5 MiB``.

    Rounding is done with integer arithmetic (half up, one decimal) so that the Python
    and TypeScript implementations always agree to the character.
    """
    if value is None:
        return "-"
    if value < 1024:
        return f"{value} B"
    index = 0
    unit = 1024
    while value >= unit * 1024 and index < len(_UNITS) - 1:
        unit *= 1024
        index += 1
    tenths = (value * 10 + unit // 2) // unit
    if tenths >= 10240 and index < len(_UNITS) - 1:  # rounded up into the next unit
        unit *= 1024
        index += 1
        tenths = (value * 10 + unit // 2) // unit
    return f"{tenths // 10}.{tenths % 10} {_UNITS[index]}"


def describe_size(size: SizeInfo | None) -> str:
    if size is None:
        return "not measured"
    text = format_bytes(size.logical_bytes)
    return text if size.complete else f">= {text}"


def describe_allocated(size: SizeInfo | None) -> str:
    if size is None or size.allocated_bytes is None:
        return "-"
    text = format_bytes(size.allocated_bytes)
    if size.allocated_method is SizeMethod.ESTIMATED:
        text = f"~{text}"
    return text


def status_label(artifact: Artifact) -> str:
    if artifact.cleanable:
        return "cleanable"
    return "review" if artifact.ambiguous else "below threshold"


# --- machine-readable -------------------------------------------------------------------


def scan_to_json(result: ScanResult) -> str:
    return result.model_dump_json(indent=2)


def scan_to_csv(result: ScanResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for artifact in result.artifacts:
        size = artifact.size
        writer.writerow(
            [
                artifact.id,
                artifact.relative_path,
                artifact.path,
                artifact.name,
                artifact.kind,
                artifact.ecosystem.value,
                artifact.rule,
                artifact.category,
                artifact.confidence.value,
                str(artifact.ambiguous).lower(),
                str(artifact.cleanable).lower(),
                artifact.review_reason or "",
                artifact.project_path,
                str(size is not None).lower(),
                str(size.complete).lower() if size else "",
                size.logical_bytes if size else "",
                size.allocated_bytes if size and size.allocated_bytes is not None else "",
                size.allocated_method.value if size else "",
                size.file_count if size else "",
                size.dir_count if size else "",
            ]
        )
    return buffer.getvalue()


def cleanup_to_json(result: CleanupResult) -> str:
    return result.model_dump_json(indent=2)


# --- terminal ---------------------------------------------------------------------------

_CONFIDENCE_STYLE = {
    Confidence.HIGH: "green",
    Confidence.MEDIUM: "yellow",
    Confidence.LOW: "red",
}
_STATUS_STYLE = {
    CleanupStatus.DELETED: "green",
    CleanupStatus.DRY_RUN: "cyan",
    CleanupStatus.PARTIAL: "yellow",
    CleanupStatus.FAILED: "red",
    CleanupStatus.REFUSED: "red",
    CleanupStatus.CANCELLED: "dim",
}


def _confidence(artifact: Artifact) -> Text:
    text = Text(artifact.confidence.value, style=_CONFIDENCE_STYLE[artifact.confidence])
    if artifact.ambiguous:
        text.append(" ?", style="bold red")
    return text


def scan_table(result: ScanResult, *, numbered: bool = False, limit: int | None = None) -> Table:
    table = Table(box=None, pad_edge=False, header_style="bold")
    if numbered:
        table.add_column("#", justify="right", style="dim")
    table.add_column("ID", style="dim", no_wrap=True)
    table.add_column("Path", overflow="fold")
    table.add_column("Type", no_wrap=True)
    table.add_column("Confidence", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Size", justify="right", no_wrap=True)
    table.add_column("On disk", justify="right", no_wrap=True)
    table.add_column("Files", justify="right", no_wrap=True)
    shown = result.artifacts if limit is None else result.artifacts[:limit]
    for index, artifact in enumerate(shown, start=1):
        status = status_label(artifact)
        row: list[RenderableType] = [
            artifact.id,
            escape(artifact.relative_path),
            artifact.rule,
            _confidence(artifact),
            Text(status, style="green" if artifact.cleanable else "yellow"),
            describe_size(artifact.size),
            describe_allocated(artifact.size),
            f"{artifact.size.file_count:,}" if artifact.size else "-",
        ]
        if numbered:
            row.insert(0, str(index))
        table.add_row(*row)
    return table


def scan_summary(result: ScanResult) -> Text:
    totals = result.totals
    stats = result.stats
    text = Text()
    text.append(f"{totals.artifact_count} artifacts", style="bold")
    text.append(f" in {escape(result.root)}\n")
    text.append(f"  detected   {format_bytes(totals.logical_bytes):>11}")
    if totals.allocated_bytes is not None:
        text.append(f"   on disk {format_bytes(totals.allocated_bytes):>11}")
    text.append(f"   {totals.file_count:,} files\n")
    text.append(f"  cleanable  {format_bytes(totals.cleanable_logical_bytes):>11}", style="green")
    text.append(f"   {totals.cleanable_count} artifacts\n", style="green")
    if totals.review_count:
        text.append(
            f"  {totals.review_count} need review and cannot be cleaned (marked ?)\n",
            style="yellow",
        )
    unmeasured = totals.artifact_count - totals.measured_count
    if unmeasured:
        text.append(f"  {unmeasured} not measured; totals exclude them\n", style="yellow")
    text.append(
        f"  scanned {stats.directories_visited:,} directories in {stats.total_ms:.0f} ms "
        f"(discovery {stats.discovery_ms:.0f} ms, sizing {stats.sizing_ms:.0f} ms)",
        style="dim",
    )
    return text


def issues_text(result: ScanResult, *, limit: int = 10) -> Text | None:
    if not result.issues:
        return None
    text = Text()
    text.append(f"{len(result.issues)} notes:\n", style="yellow")
    for issue in result.issues[:limit]:
        text.append(f"  {issue.kind}: ", style="yellow")
        text.append(f"{issue.path} - {issue.message}\n")
    if len(result.issues) > limit:
        text.append(f"  ... and {len(result.issues) - limit} more\n", style="dim")
    return text


def scan_report(result: ScanResult, *, numbered: bool = False) -> RenderableType:
    """The full terminal report for a scan."""
    parts: list[RenderableType] = []
    if result.artifacts:
        parts.append(scan_table(result, numbered=numbered))
        parts.append(Text())
    else:
        parts.append(Text("No development artifacts found.", style="bold"))
    parts.append(scan_summary(result))
    issues = issues_text(result)
    if issues is not None:
        parts.append(Text())
        parts.append(issues)
    return Group(*parts)


def plan_report(plan: CleanupPlan) -> RenderableType:
    """The cleanup preview: exactly what would be deleted, and what was refused."""
    parts: list[RenderableType] = []
    deletable = plan.deletable
    refused = plan.refused
    verb = "Would delete" if plan.dry_run else "Will delete"
    if deletable:
        table = Table(
            title=f"{verb} {len(deletable)} artifact(s)",
            title_justify="left",
            title_style="bold",
            box=None,
            pad_edge=False,
            header_style="bold",
        )
        table.add_column("Path", overflow="fold")
        table.add_column("Type", no_wrap=True)
        table.add_column("Confidence", no_wrap=True)
        table.add_column("Files", justify="right", no_wrap=True)
        table.add_column("Size", justify="right", no_wrap=True)
        for item in deletable:
            table.add_row(
                escape(item.path),
                item.rule,
                Text(item.confidence.value, style=_CONFIDENCE_STYLE[item.confidence]),
                f"{item.file_count:,}",
                format_bytes(item.logical_bytes),
            )
        parts.append(table)
    else:
        parts.append(Text("Nothing in the selection can be deleted.", style="bold"))
    if refused:
        table = Table(
            title=f"Refused: {len(refused)} selected artifact(s) will NOT be deleted",
            title_justify="left",
            title_style="bold yellow",
            box=None,
            pad_edge=False,
            header_style="bold",
        )
        table.add_column("Path", overflow="fold")
        table.add_column("Reason", overflow="fold")
        for item in refused:
            table.add_row(escape(item.path), escape(item.refusal_reason or "refused"))
        parts.append(Text())
        parts.append(table)
    totals = plan.totals
    summary = Text()
    summary.append(f"\n  detected  {format_bytes(totals.detected_bytes):>11}")
    summary.append(f"   {totals.detected_count} artifacts in the scan\n")
    summary.append(f"  selected  {format_bytes(totals.selected_bytes):>11}")
    summary.append(f"   {totals.selected_count} artifacts\n")
    summary.append(
        f"  to delete {format_bytes(totals.deletable_bytes):>11}"
        f"   {totals.deletable_count} artifacts, {totals.deletable_file_count:,} files",
        style="bold",
    )
    parts.append(summary)
    return Group(*parts)


def cleanup_report(result: CleanupResult) -> RenderableType:
    """What a cleanup actually did, with the four sizes kept apart."""
    table = Table(box=None, pad_edge=False, header_style="bold")
    table.add_column("Path", overflow="fold")
    table.add_column("Result", no_wrap=True)
    table.add_column("Deleted", justify="right", no_wrap=True)
    table.add_column("Failed", justify="right", no_wrap=True)
    table.add_column("Note", overflow="fold")
    for item in result.items:
        table.add_row(
            escape(item.path),
            Text(item.status.value, style=_STATUS_STYLE[item.status]),
            format_bytes(item.deleted_bytes),
            format_bytes(item.failed_bytes) if item.failed_bytes else "-",
            escape(item.message or ""),
        )
    totals = result.totals
    summary = Text()
    if result.cancelled:
        summary.append("\nCancelled - nothing was deleted.\n", style="bold")
    elif result.dry_run:
        summary.append("\nDry run - nothing was deleted.\n", style="bold cyan")
    summary.append(f"  detected      {format_bytes(totals.detected_bytes):>11}\n")
    summary.append(f"  selected      {format_bytes(totals.selected_bytes):>11}\n")
    if result.dry_run:
        summary.append(
            f"  would delete  {format_bytes(totals.would_delete_bytes):>11}\n", style="cyan"
        )
    summary.append(
        f"  deleted       {format_bytes(totals.deleted_bytes):>11}"
        f"   {totals.deleted_count} artifacts\n",
        style="green" if totals.deleted_bytes else "",
    )
    summary.append(
        f"  failed        {format_bytes(totals.failed_bytes):>11}"
        f"   {totals.failed_count} artifacts\n",
        style="red" if totals.failed_count else "",
    )
    summary.append(
        f"  refused       {format_bytes(totals.refused_bytes):>11}"
        f"   {totals.refused_count} artifacts",
        style="yellow" if totals.refused_count else "",
    )
    parts: list[RenderableType] = [table, summary]
    for item in result.items:
        for error in item.errors[:5]:
            if item.status in (CleanupStatus.PARTIAL, CleanupStatus.FAILED):
                parts.append(Text(f"  ! {error}", style="red"))
    return Group(*parts)


def detail_report(detail: ArtifactDetail) -> RenderableType:
    """Everything known about one artifact, for ``sift inspect``."""
    artifact = detail.artifact
    info = Table.grid(padding=(0, 2))
    info.add_column(style="bold")
    info.add_column(overflow="fold")
    info.add_row("ID", artifact.id)
    info.add_row("Path", escape(artifact.path))
    info.add_row("Type", f"{artifact.rule} ({artifact.ecosystem.value}, {artifact.category})")
    info.add_row("What it is", escape(artifact.description))
    info.add_row("Project", escape(artifact.project_path))
    info.add_row("Confidence", _confidence(artifact))
    info.add_row("Status", status_label(artifact))
    if artifact.review_reason:
        info.add_row("Why", escape(artifact.review_reason))
    if not detail.exists:
        info.add_row("On disk now", Text("no longer exists", style="red"))
    else:
        size = detail.current_size
        info.add_row("Size", describe_size(size))
        info.add_row("On disk", describe_allocated(size))
        if size is not None:
            info.add_row(
                "Contents",
                f"{size.file_count:,} files, {size.dir_count:,} directories, "
                f"{size.link_count:,} links",
            )
        if not detail.still_matches:
            info.add_row("Changed", Text("no longer matches the scanned type", style="red"))
    if detail.cleanup_allowed:
        info.add_row("Cleanup", Text("allowed (after preview and confirmation)", style="green"))
    else:
        info.add_row(
            "Cleanup", Text(f"refused: {detail.cleanup_refusal or 'not allowed'}", style="yellow")
        )

    parts: list[RenderableType] = [info, Text("\nEvidence", style="bold")]
    for evidence in artifact.evidence:
        parts.append(Text(f"  [{evidence.kind}] {evidence.detail}"))
    if detail.children:
        children = Table(
            title="\nLargest entries inside",
            title_justify="left",
            title_style="bold",
            box=None,
            pad_edge=False,
        )
        children.add_column("Name", overflow="fold")
        children.add_column("Kind")
        children.add_column("Size", justify="right")
        children.add_column("Files", justify="right")
        for child in detail.children:
            children.add_row(
                escape(child.name),
                child.kind,
                format_bytes(child.logical_bytes),
                f"{child.file_count:,}",
            )
        parts.append(children)
    return Group(*parts)
