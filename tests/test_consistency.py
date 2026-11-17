"""One fixture, every interface, one answer.

The shared fixture workspace is scanned through the core scanner, the service, the CLI
(in-process and as a real subprocess), the HTTP API and the TUI. Each must report exactly
the same artifacts, in the same order, with the same paths, classifications and sizes.
The dashboard is covered from both sides: its TypeScript types and its recorded test data
must match what the API returns today.
"""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from textual.widgets import DataTable
from typer.testing import CliRunner

from fixture_workspace import Workspace
from sift.api.app import create_app
from sift.cli.app import app as cli_app
from sift.core import reports
from sift.core.models import Artifact, ScanOptions, ScanResult
from sift.core.scanner import scan
from sift.core.service import SiftService
from sift.tui.app import SiftApp

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))


@dataclass(frozen=True)
class Row:
    """What every interface must agree on for one artifact."""

    id: str
    path: str
    relative_path: str
    rule: str
    ecosystem: str
    confidence: str
    ambiguous: bool
    cleanable: bool
    logical_bytes: int
    allocated_bytes: int | None
    file_count: int


def row(artifact: Artifact) -> Row:
    assert artifact.size is not None
    return Row(
        id=artifact.id,
        path=artifact.path,
        relative_path=artifact.relative_path,
        rule=artifact.rule,
        ecosystem=artifact.ecosystem.value,
        confidence=artifact.confidence.value,
        ambiguous=artifact.ambiguous,
        cleanable=artifact.cleanable,
        logical_bytes=artifact.size.logical_bytes,
        allocated_bytes=artifact.size.allocated_bytes,
        file_count=artifact.size.file_count,
    )


def rows_from_json(data: dict) -> list[Row]:
    return [row(a) for a in ScanResult.model_validate(data).artifacts]


@pytest.fixture(scope="module", params=[False, True], ids=["single-project", "recursive"])
def reference(request: pytest.FixtureRequest, workspace_ro: Workspace) -> tuple[bool, list[Row]]:
    """The core scanner's answer, checked against the fixture's own expectations."""
    recursive: bool = request.param
    result = scan(workspace_ro.root, ScanOptions(recursive=recursive))
    expected = workspace_ro.expect(recursive=recursive)
    assert {a.relative_path for a in result.artifacts} == set(expected)
    for artifact in result.artifacts:
        want = expected[artifact.relative_path]
        assert artifact.size is not None
        assert (artifact.rule, artifact.size.logical_bytes, artifact.size.file_count) == (
            want.rule,
            want.logical_bytes,
            want.file_count,
        )
    return recursive, [row(a) for a in result.artifacts]


def test_service(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    recursive, expected = reference
    service = SiftService()
    result = service.scan(workspace_ro.root, service.scan_options(recursive=recursive))
    assert [row(a) for a in result.artifacts] == expected


def test_cli_json(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    recursive, expected = reference
    args = ["scan", str(workspace_ro.root), "--format", "json"] + (["-r"] if recursive else [])
    result = CliRunner().invoke(cli_app, args)
    assert result.exit_code == 0, result.output
    assert rows_from_json(json.loads(result.stdout)) == expected


def test_cli_csv(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    recursive, expected = reference
    args = ["scan", str(workspace_ro.root), "--format", "csv"] + (["-r"] if recursive else [])
    result = CliRunner().invoke(cli_app, args)
    got = [
        (
            r["id"],
            r["path"],
            r["relative_path"],
            r["rule"],
            r["ecosystem"],
            r["confidence"],
            r["ambiguous"] == "true",
            r["cleanable"] == "true",
            int(r["logical_bytes"]),
            int(r["allocated_bytes"]) if r["allocated_bytes"] else None,
            int(r["file_count"]),
        )
        for r in csv.DictReader(io.StringIO(result.stdout))
    ]
    assert got == [tuple(vars(e).values()) for e in expected]


def test_cli_table(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    """The human-readable table shows the same IDs, paths, types and sizes."""
    recursive, expected = reference
    args = ["scan", str(workspace_ro.root)] + (["-r"] if recursive else [])
    result = CliRunner().invoke(cli_app, args, env={"COLUMNS": "250"})
    lines = [line.split() for line in result.stdout.splitlines()]
    by_id = {parts[0]: parts for parts in lines if parts and len(parts[0]) == 12}
    assert list(by_id) == [e.id for e in expected]
    for want in expected:
        text = " ".join(by_id[want.id])
        assert want.relative_path in text
        assert want.rule in text
        assert want.confidence in text
        assert reports.format_bytes(want.logical_bytes) in text
        assert f" {want.file_count:,}" in text


def test_cli_subprocess(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    recursive, expected = reference
    args = [sys.executable, "-m", "sift", "scan", str(workspace_ro.root), "--format", "json"]
    done = subprocess.run(
        args + (["--recursive"] if recursive else []),
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        timeout=120,
        check=True,
    )
    assert rows_from_json(json.loads(done.stdout)) == expected


def test_api(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    recursive, expected = reference
    service = SiftService(roots=[str(workspace_ro.root)])
    api = TestClient(
        create_app(service, static_dir=REPO / "no-static"), base_url="http://127.0.0.1:8765"
    )
    response = api.post("/api/scans", json={"path": str(workspace_ro.root), "recursive": recursive})
    assert response.status_code == 200, response.text
    data = response.json()
    assert rows_from_json(data) == expected
    # the stored copy and both export formats say the same thing
    assert rows_from_json(api.get(f"/api/scans/{data['id']}").json()) == expected
    assert rows_from_json(api.get(f"/api/scans/{data['id']}/report").json()) == expected
    exported = csv.DictReader(
        io.StringIO(api.get(f"/api/scans/{data['id']}/report?format=csv").text)
    )
    assert [(r["id"], int(r["logical_bytes"])) for r in exported] == [
        (e.id, e.logical_bytes) for e in expected
    ]
    # and so does each artifact when inspected individually
    for want in expected[:6]:
        detail = api.get(f"/api/scans/{data['id']}/artifacts/{want.id}").json()
        assert detail["artifact"]["path"] == want.path
        assert detail["current_size"]["logical_bytes"] == want.logical_bytes


async def test_tui(reference: tuple[bool, list[Row]], workspace_ro: Workspace) -> None:
    recursive, expected = reference
    tui = SiftApp(SiftService(), workspace_ro.root, recursive=recursive)
    async with tui.run_test(size=(160, 50)) as pilot:
        for _ in range(3):
            await tui.workers.wait_for_complete()
            await pilot.pause()
        assert tui.result is not None
        assert [row(a) for a in tui.result.artifacts] == expected
        table = tui.query_one("#artifacts", DataTable)
        shown = [[str(cell) for cell in table.get_row_at(i)] for i in range(table.row_count)]
    assert len(shown) == len(expected)
    for cells, want in zip(shown, expected, strict=True):
        _selected, size, allocated, files, confidence, status, rule, path = cells
        assert path == want.relative_path
        assert rule == want.rule
        assert size == reports.format_bytes(want.logical_bytes)
        assert allocated.lstrip("~") == reports.format_bytes(want.allocated_bytes)
        assert files == f"{want.file_count:,}"
        assert confidence == want.confidence + (" ?" if want.ambiguous else "")
        assert (status == "cleanable") is want.cleanable


# --- the dashboard ------------------------------------------------------------------------


def test_dashboard_types_match_the_api() -> None:
    """web/src/api/schema.ts is generated from the API; it must not be stale."""
    import gen_api_types

    current = gen_api_types.TARGET.read_text(encoding="utf-8")
    assert current == gen_api_types.render(), "run: python scripts/gen_api_types.py"


def test_dashboard_test_data_is_what_the_scanner_reports(workspace_ro: Workspace) -> None:
    """The dashboard tests render web/src/test/fixture.json.

    It was recorded from the real API (scripts/record_fixture_scan.py). Check that it still
    describes the fixture the way the scanner does today, so those tests cannot drift into
    asserting on stale or invented numbers. Paths, IDs and allocated sizes depend on the
    machine that recorded the file, so only the portable fields are compared.
    """
    recorded = json.loads((REPO / "web" / "src" / "test" / "fixture.json").read_text("utf-8"))
    live = scan(workspace_ro.root, ScanOptions(recursive=True))

    def portable(artifacts: list[dict]) -> list[tuple]:
        return sorted(
            (
                a["relative_path"],
                a["rule"],
                a["ecosystem"],
                a["category"],
                a["confidence"],
                a["ambiguous"],
                a["cleanable"],
                a["kind"],
                a["size"]["logical_bytes"],
                a["size"]["file_count"],
                a["size"]["dir_count"],
            )
            for a in artifacts
        )

    assert portable(recorded["scan"]["artifacts"]) == portable(
        json.loads(live.model_dump_json())["artifacts"]
    )
    totals = recorded["scan"]["totals"]
    assert totals["logical_bytes"] == live.totals.logical_bytes
    assert totals["cleanable_logical_bytes"] == live.totals.cleanable_logical_bytes
    assert totals["artifact_count"] == live.totals.artifact_count
    assert totals["cleanable_count"] == live.totals.cleanable_count
    # the recorded responses still parse as today's models
    ScanResult.model_validate(recorded["scan"])
    plan = recorded["plan"]
    assert {i["relative_path"]: i["allowed"] for i in plan["items"]} == {
        "rust-app/target": True,
        "node-app/.next": True,
        "ambiguous/build": False,
    }
    assert plan["totals"]["deletable_bytes"] == 16307 + 8213


def test_size_formatting_table_is_shared_with_the_dashboard() -> None:
    """Both test suites read tests/shared/format_bytes.json; the dashboard's must import it."""
    source = (REPO / "web" / "src" / "lib" / "format.test.ts").read_text("utf-8")
    assert "tests/shared/format_bytes.json" in source
    cases = json.loads((REPO / "tests" / "shared" / "format_bytes.json").read_text("utf-8"))
    assert len(cases) >= 20
    for value, text in cases:
        assert reports.format_bytes(value) == text
