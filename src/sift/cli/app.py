"""The ``sift`` command.

This module only parses arguments, asks questions and prints. Scanning, validation and
deletion all happen in :mod:`sift.core` through :class:`~sift.core.service.SiftService`.

Exit codes:

* 0 - success, including dry runs and cleanups the user cancelled
* 1 - something went wrong, or some of a cleanup could not be deleted
* 2 - bad usage: unknown option, missing path, invalid configuration
* 3 - a cleanup was refused for safety (nothing in it failed)
"""

from __future__ import annotations

import json
import sys
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console
from rich.text import Text

from sift import __version__
from sift.core import reports
from sift.core.cleanup import CONFIRMATION_WORD, PERMANENT_NOTICE
from sift.core.config import (
    LOOPBACK_HOSTS,
    ConfigError,
    config_path,
    dump_config,
    write_default_config,
)
from sift.core.models import CleanupItem, Confidence, ScanResult
from sift.core.scanner import ScanProgress, resolve_root
from sift.core.service import ArtifactNotFound, ScanError, SiftService, UnsafeOperation

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_UNSAFE = 3
_WIDE = 120  # terminal width below which tables drop their less important columns

app = typer.Typer(
    name="sift",
    help="Find the clutter. Keep the code. Discover and safely clean development artifacts.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Show or create the configuration file.", no_args_is_help=True)
app.add_typer(config_app, name="config")


class ScanFormat(StrEnum):
    table = "table"
    json = "json"
    csv = "csv"


class DetailFormat(StrEnum):
    table = "table"
    json = "json"


class ConfigFormat(StrEnum):
    toml = "toml"
    json = "json"


PathArgument = Annotated[
    Path, typer.Argument(help="Directory to scan.", show_default=False, metavar="PATH")
]
RecursiveOption = Annotated[
    bool,
    typer.Option(
        "--recursive",
        "-r",
        help="Scan every project under PATH, including nested repositories.",
    ),
]
DepthOption = Annotated[
    int | None,
    typer.Option("--depth", min=0, help="How many directory levels below PATH to descend."),
]
ExcludeOption = Annotated[
    list[str] | None,
    typer.Option("--exclude", "-x", help="gitignore-style pattern to skip. Repeatable."),
]
ConfidenceOption = Annotated[
    Confidence | None,
    typer.Option(
        "--min-confidence",
        help="Lowest confidence that counts as cleanable. Ambiguous artifacts never do.",
    ),
]


def _out() -> Console:
    return Console(highlight=False)


def _err() -> Console:
    return Console(stderr=True, soft_wrap=True, highlight=False)


def _fail(message: str, code: int = EXIT_ERROR) -> typer.Exit:
    _err().print(Text(f"error: {message}", style="bold red"))
    return typer.Exit(code)


def _service(**kwargs: object) -> SiftService:
    try:
        return SiftService(**kwargs)  # type: ignore[arg-type]
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    except ScanError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


def _write_raw(text: str) -> None:
    """Machine-readable output goes to stdout untouched by Rich."""
    stream = sys.stdout
    stream.write(text if text.endswith("\n") else text + "\n")
    stream.flush()


def _run_scan(
    service: SiftService,
    path: Path,
    *,
    recursive: bool,
    depth: int | None,
    exclude: list[str] | None,
    min_confidence: Confidence | None,
    time_budget: float | None = None,
    measure: bool = True,
    record: bool = True,
    quiet: bool = False,
) -> ScanResult:
    options = service.scan_options(
        recursive=recursive,
        max_depth=depth,
        exclude=exclude or (),
        min_confidence=min_confidence,
        time_budget=time_budget,
        measure=measure,
    )
    console = _err()
    try:
        if quiet or not console.is_terminal:
            return service.scan(path, options, record=record)
        with console.status("Looking for artifacts...") as status:

            def progress(event: ScanProgress) -> None:
                if event.phase == "discovery":
                    status.update(
                        f"Looking for artifacts... {event.directories_visited:,} directories, "
                        f"{event.artifacts_found} found"
                    )
                elif event.phase == "sizing":
                    status.update(
                        f"Measuring... {event.artifacts_measured}/{event.artifacts_found}"
                    )

            return service.scan(path, options, progress=progress, record=record)
    except ScanError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc


def _version(value: bool) -> None:
    if value:
        typer.echo(f"sift {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show the version."),
    ] = False,
) -> None:
    pass


# --- scan -----------------------------------------------------------------------------


@app.command()
def scan(
    path: PathArgument,
    recursive: RecursiveOption = False,
    fmt: Annotated[
        ScanFormat, typer.Option("--format", "-f", help="Output format.")
    ] = ScanFormat.table,
    depth: DepthOption = None,
    exclude: ExcludeOption = None,
    min_confidence: ConfidenceOption = None,
    time_budget: Annotated[
        float | None,
        typer.Option(
            "--time-budget",
            min=0.001,
            help="Stop measuring after this many seconds; the rest is reported as not measured.",
        ),
    ] = None,
    no_size: Annotated[
        bool, typer.Option("--no-size", help="Only find artifacts; do not measure them.")
    ] = False,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write the report to this file instead of stdout."),
    ] = None,
    no_history: Annotated[
        bool, typer.Option("--no-history", help="Do not record this scan in local history.")
    ] = False,
) -> None:
    """Find development artifacts under PATH and report their sizes. Read-only."""
    service = _service()
    result = _run_scan(
        service,
        path,
        recursive=recursive,
        depth=depth,
        exclude=exclude,
        min_confidence=min_confidence,
        time_budget=time_budget,
        measure=not no_size,
        record=not no_history,
        quiet=fmt is not ScanFormat.table,
    )
    if fmt is ScanFormat.table:
        if output is not None:
            with output.open("w", encoding="utf-8") as handle:
                Console(file=handle, width=160, highlight=False).print(reports.scan_report(result))
        else:
            out = _out()
            out.print(reports.scan_report(result, compact=out.width < _WIDE))
    else:
        text = service.export_scan(result, "json" if fmt is ScanFormat.json else "csv")
        if output is not None:
            output.write_text(text, encoding="utf-8", newline="\n")
        else:
            _write_raw(text)
    if output is not None:
        _err().print(f"Report written to {output}")


# --- inspect --------------------------------------------------------------------------


@app.command()
def inspect(
    artifact_id: Annotated[
        str, typer.Argument(help="Artifact ID, as shown by `sift scan`.", metavar="ARTIFACT_ID")
    ],
    root: Annotated[
        Path | None,
        typer.Option(
            "--root",
            help="Scan this directory first instead of looking the ID up in scan history.",
        ),
    ] = None,
    fmt: Annotated[
        DetailFormat, typer.Option("--format", "-f", help="Output format.")
    ] = DetailFormat.table,
) -> None:
    """Show everything Sift knows about one artifact, re-checked on disk."""
    service = _service()
    scan_id: str | None = None
    if root is not None:
        fresh = _run_scan(
            service,
            root,
            recursive=True,
            depth=None,
            exclude=None,
            min_confidence=None,
            record=False,
            quiet=fmt is not DetailFormat.table,
        )
        scan_id = fresh.id
    try:
        detail = service.inspect(artifact_id, scan_id)
    except ArtifactNotFound:
        where = f"under {root}" if root is not None else "in scan history"
        hint = (
            ""
            if root is not None
            else " Run `sift scan PATH` first, or pass --root PATH to scan now."
        )
        raise _fail(f"no artifact with ID {artifact_id} {where}.{hint}") from None
    if fmt is DetailFormat.json:
        _write_raw(detail.model_dump_json(indent=2))
    else:
        _out().print(reports.detail_report(detail))


# --- clean ----------------------------------------------------------------------------


def parse_selection(text: str, count: int) -> list[int] | None:
    """Turn ``"1,3-5"`` into zero-based indexes. ``None`` means "all cleanable".

    Raises ValueError for anything that is not a number or range within 1..count.
    """
    cleaned = text.strip().lower()
    if cleaned == "all":
        return None
    chosen: dict[int, None] = {}
    for part in cleaned.replace(",", " ").split():
        first, dash, last = part.partition("-")
        if not first.isdigit() or (dash and not last.isdigit()):
            raise ValueError(f"'{part}' is not a number or a range like 2-4")
        low, high = int(first), int(last) if dash else int(first)
        if low > high:
            raise ValueError(f"'{part}' runs backwards")
        if low < 1 or high > count:
            raise ValueError(f"'{part}' is outside 1-{count}")
        for number in range(low, high + 1):
            chosen.setdefault(number - 1, None)
    return list(chosen)


def _ask(prompt: str) -> str | None:
    """Read one line. ``None`` means the user backed out (Ctrl+C or end of input)."""
    try:
        answer: str = click.prompt(
            prompt, default="", show_default=False, prompt_suffix=" ", err=True
        )
    except (click.Abort, EOFError, KeyboardInterrupt):
        return None
    return answer.strip()


def _choose(result: ScanResult) -> list[str] | None:
    """Ask which artifacts to clean. ``None`` means the user cancelled."""
    console = _err()
    count = len(result.artifacts)
    for _ in range(3):
        answer = _ask(
            f"Select artifacts to clean - numbers like 1,3-5, 'all' for every cleanable one, "
            f"or Enter to cancel [1-{count}]:"
        )
        if not answer:
            return None
        try:
            indexes = parse_selection(answer, count)
        except ValueError as exc:
            console.print(Text(f"Not understood: {exc}", style="yellow"))
            continue
        if indexes is None:
            return [a.id for a in result.artifacts if a.cleanable]
        return [result.artifacts[i].id for i in indexes]
    return None


@app.command()
def clean(
    path: PathArgument,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", "-n", help="Show what would be deleted. Deletes nothing."),
    ] = False,
    interactive: Annotated[
        bool,
        typer.Option(
            "--interactive",
            "-i",
            help="Choose artifacts, review the preview, then confirm before anything is deleted.",
        ),
    ] = False,
    recursive: RecursiveOption = False,
    depth: DepthOption = None,
    exclude: ExcludeOption = None,
    min_confidence: ConfidenceOption = None,
) -> None:
    """Preview or interactively clean artifacts under PATH.

    Nothing is ever deleted without --interactive, an explicit selection, a preview and a
    typed confirmation. Without --interactive this command only shows a dry run.
    """
    service = _service()
    out = _out()
    err = _err()
    result = _run_scan(
        service,
        path,
        recursive=recursive,
        depth=depth,
        exclude=exclude,
        min_confidence=min_confidence,
    )
    if not result.artifacts:
        out.print(reports.scan_report(result))
        return

    out.print(reports.scan_report(result, numbered=interactive, compact=out.width < _WIDE))
    out.print()

    if interactive:
        selected = _choose(result)
        if selected is None:
            out.print(Text("Cancelled - nothing was deleted.", style="bold"))
            return
        if not selected:
            out.print(Text("No cleanable artifacts to select - nothing was deleted.", style="bold"))
            return
    else:
        selected = [a.id for a in result.artifacts if a.cleanable]
        if not dry_run:
            err.print(
                Text(
                    "No cleanup mode given, so this is a dry run. "
                    "Use --interactive to choose what to delete.",
                    style="yellow",
                )
            )

    plan = service.plan_cleanup(result, selected, dry_run=dry_run or not interactive)
    out.print(reports.plan_report(plan))
    out.print()

    if plan.dry_run:
        outcome = service.execute_cleanup(plan)
        out.print(reports.cleanup_report(outcome))
        return

    if not plan.deletable:
        service.cancel_cleanup(plan)
        out.print(Text("Nothing was deleted: every selected artifact was refused.", style="bold"))
        raise typer.Exit(EXIT_UNSAFE)

    # The final gate. Everything about to be removed is listed above; Enter backs out.
    totals = plan.totals
    err.print(Text(PERMANENT_NOTICE, style="bold red"))
    answer = _ask(
        f'Type "{CONFIRMATION_WORD}" to permanently delete {totals.deletable_count} artifact(s) '
        f"({reports.format_bytes(totals.deletable_bytes)}, "
        f"{totals.deletable_file_count:,} files), or press Enter to cancel:"
    )
    if answer is None or answer.lower() != CONFIRMATION_WORD:
        service.cancel_cleanup(plan)
        out.print(Text("Cancelled - nothing was deleted.", style="bold"))
        return

    def progress(index: int, total: int, item: CleanupItem) -> None:
        if item.allowed:
            err.print(f"[{index + 1}/{total}] deleting {item.relative_path}", markup=False)

    try:
        outcome = service.execute_cleanup(plan, plan.confirmation_token, progress=progress)
    except UnsafeOperation as exc:
        raise _fail(f"cleanup refused: {exc.reason}", EXIT_UNSAFE) from exc
    out.print()
    out.print(reports.cleanup_report(outcome))
    if outcome.totals.failed_count:
        raise typer.Exit(EXIT_ERROR)
    if outcome.totals.refused_count:
        raise typer.Exit(EXIT_UNSAFE)


# --- config ---------------------------------------------------------------------------


@config_app.command("show")
def config_show(
    fmt: Annotated[
        ConfigFormat, typer.Option("--format", "-f", help="Output format.")
    ] = ConfigFormat.toml,
) -> None:
    """Print the configuration in effect, and where it is read from."""
    service = _service()
    target = config_path()
    if fmt is ConfigFormat.json:
        payload = {
            "path": str(target),
            "exists": target.exists(),
            "config": service.config.model_dump(mode="json"),
        }
        _write_raw(json.dumps(payload, indent=2))
        return
    state = "" if target.exists() else " (not created yet; these are the defaults)"
    _err().print(f"# {target}{state}", markup=False)
    _write_raw(dump_config(service.config))


@config_app.command("init")
def config_init(
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing config file.")
    ] = False,
) -> None:
    """Create a commented starter config file."""
    try:
        target = write_default_config(force=force)
    except FileExistsError as exc:
        raise _fail(f"{exc} already exists. Use --force to overwrite it.") from None
    except OSError as exc:
        raise _fail(f"could not write the config file: {exc}") from exc
    typer.echo(f"Created {target}")


@config_app.command("path")
def config_where() -> None:
    """Print the config file location."""
    _write_raw(str(config_path()))


# --- other interfaces -----------------------------------------------------------------


@app.command()
def tui(
    path: Annotated[Path, typer.Argument(help="Directory to scan.")] = Path("."),
    recursive: RecursiveOption = False,
) -> None:
    """Browse and clean artifacts in a full-screen terminal interface."""
    service = _service()
    try:
        resolve_root(path)
    except ScanError as exc:
        raise _fail(str(exc), EXIT_USAGE) from exc
    from sift.tui.app import SiftApp

    SiftApp(service, path, recursive=recursive).run()


@app.command()
def serve(
    root: Annotated[
        list[Path] | None,
        typer.Option(
            "--root",
            help="Directory the dashboard may scan and clean. Repeatable; adds to config roots.",
        ),
    ] = None,
    host: Annotated[str | None, typer.Option("--host", help="Loopback address to bind.")] = None,
    port: Annotated[int | None, typer.Option("--port", min=1, max=65535)] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the dashboard in a browser.")
    ] = False,
) -> None:
    """Run the local web dashboard and API."""
    service = _service(roots=[str(r) for r in root or []])
    bind_host = host or service.config.server.host
    bind_port = port or service.config.server.port
    if bind_host not in LOOPBACK_HOSTS:
        raise _fail(
            "the dashboard has no authentication and only binds to loopback addresses "
            f"({', '.join(sorted(LOOPBACK_HOSTS))})",
            EXIT_USAGE,
        )
    del bind_port, open_browser
    raise _fail("the web dashboard is not implemented yet")


def main() -> None:
    # Paths can contain any character; make sure piping the output never fails on one.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    main()
