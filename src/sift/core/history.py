"""Optional local history: past scans and cleanup audits in a SQLite database.

Everything stays on this machine, in Sift's data directory. History is what lets
``sift inspect ARTIFACT_ID`` find an artifact from an earlier scan, and what the dashboard
lists on its history page. It can be turned off in the config.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sift.core.models import CleanupResult, CleanupSummary, ScanResult, ScanSummary

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    started_at TEXT NOT NULL,
    recursive INTEGER NOT NULL,
    artifact_count INTEGER NOT NULL,
    logical_bytes INTEGER NOT NULL,
    allocated_bytes INTEGER,
    total_ms REAL NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scan_artifacts (
    scan_id TEXT NOT NULL REFERENCES scans(id) ON DELETE CASCADE,
    artifact_id TEXT NOT NULL,
    PRIMARY KEY (scan_id, artifact_id)
);
CREATE INDEX IF NOT EXISTS scan_artifacts_by_artifact ON scan_artifacts(artifact_id);
CREATE TABLE IF NOT EXISTS cleanups (
    id TEXT PRIMARY KEY,
    scan_id TEXT NOT NULL,
    root TEXT NOT NULL,
    started_at TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    selected_bytes INTEGER NOT NULL,
    deleted_bytes INTEGER NOT NULL,
    failed_bytes INTEGER NOT NULL,
    item_count INTEGER NOT NULL,
    result TEXT NOT NULL
);
"""


class History:
    def __init__(self, path: Path, *, max_entries: int = 100) -> None:
        self.path = path
        self.max_entries = max_entries
        self._ready = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self._ready:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            if not self._ready:
                connection.executescript(_SCHEMA)
                self._ready = True
            with connection:
                yield connection
        finally:
            connection.close()

    # --- scans --------------------------------------------------------------------------

    def save_scan(self, result: ScanResult) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO scans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.id,
                    result.root,
                    result.started_at.isoformat(),
                    int(result.options.recursive),
                    result.totals.artifact_count,
                    result.totals.logical_bytes,
                    result.totals.allocated_bytes,
                    result.stats.total_ms,
                    result.model_dump_json(),
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO scan_artifacts VALUES (?, ?)",
                [(result.id, artifact.id) for artifact in result.artifacts],
            )
            db.execute(
                "DELETE FROM scans WHERE id NOT IN "
                "(SELECT id FROM scans ORDER BY started_at DESC, rowid DESC LIMIT ?)",
                (self.max_entries,),
            )

    def get_scan(self, scan_id: str) -> ScanResult | None:
        with self._connect() as db:
            row = db.execute("SELECT result FROM scans WHERE id = ?", (scan_id,)).fetchone()
        return ScanResult.model_validate_json(row[0]) if row else None

    def latest_scan(self) -> ScanResult | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT result FROM scans ORDER BY started_at DESC, rowid DESC LIMIT 1"
            ).fetchone()
        return ScanResult.model_validate_json(row[0]) if row else None

    def find_artifact(self, artifact_id: str) -> ScanResult | None:
        """The most recent scan that contains ``artifact_id``."""
        with self._connect() as db:
            row = db.execute(
                "SELECT s.result FROM scans s JOIN scan_artifacts a ON a.scan_id = s.id "
                "WHERE a.artifact_id = ? ORDER BY s.started_at DESC, s.rowid DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
        return ScanResult.model_validate_json(row[0]) if row else None

    def list_scans(self, limit: int = 50) -> list[ScanSummary]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, root, started_at, recursive, artifact_count, logical_bytes, "
                "allocated_bytes, total_ms FROM scans "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            ScanSummary(
                id=row[0],
                root=row[1],
                started_at=row[2],
                recursive=bool(row[3]),
                artifact_count=row[4],
                logical_bytes=row[5],
                allocated_bytes=row[6],
                total_ms=row[7],
            )
            for row in rows
        ]

    # --- cleanups -----------------------------------------------------------------------

    def save_cleanup(self, result: CleanupResult) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cleanups VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    result.id,
                    result.scan_id,
                    result.root,
                    result.started_at.isoformat(),
                    int(result.dry_run),
                    int(result.cancelled),
                    result.totals.selected_bytes,
                    result.totals.deleted_bytes,
                    result.totals.failed_bytes,
                    len(result.items),
                    result.model_dump_json(),
                ),
            )
            db.execute(
                "DELETE FROM cleanups WHERE id NOT IN "
                "(SELECT id FROM cleanups ORDER BY started_at DESC, rowid DESC LIMIT ?)",
                (self.max_entries,),
            )

    def get_cleanup(self, cleanup_id: str) -> CleanupResult | None:
        with self._connect() as db:
            row = db.execute("SELECT result FROM cleanups WHERE id = ?", (cleanup_id,)).fetchone()
        return CleanupResult.model_validate_json(row[0]) if row else None

    def list_cleanups(self, limit: int = 50) -> list[CleanupSummary]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, scan_id, root, started_at, dry_run, cancelled, selected_bytes, "
                "deleted_bytes, failed_bytes, item_count FROM cleanups "
                "ORDER BY started_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            CleanupSummary(
                id=row[0],
                scan_id=row[1],
                root=row[2],
                started_at=row[3],
                dry_run=bool(row[4]),
                cancelled=bool(row[5]),
                selected_bytes=row[6],
                deleted_bytes=row[7],
                failed_bytes=row[8],
                item_count=row[9],
            )
            for row in rows
        ]

    def clear(self) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM scans")
            db.execute("DELETE FROM cleanups")
