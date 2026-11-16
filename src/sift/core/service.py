"""The one façade every interface uses.

The CLI, the TUI and the HTTP API all hold a :class:`SiftService` and call the same
methods, so a scan, an inspection or a cleanup behaves identically wherever it is
started. Nothing outside ``sift.core`` should import the scanner or cleanup modules
directly.
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from sift.core import cleanup, reports, scanner, sizing
from sift.core.config import SiftConfig, data_dir, load_config, save_config
from sift.core.detectors import RuleSet, build_ruleset, is_link
from sift.core.history import History
from sift.core.models import (
    ArtifactDetail,
    ChildEntry,
    CleanupItem,
    CleanupPlan,
    CleanupResult,
    CleanupSummary,
    Confidence,
    ScanOptions,
    ScanResult,
    ScanSummary,
)
from sift.core.safety import UnsafeOperation
from sift.core.scanner import ProgressCallback, ScanError

__all__ = [
    "ArtifactNotFound",
    "PathNotAllowed",
    "PlanNotFound",
    "ScanError",
    "ScanNotFound",
    "SiftService",
    "UnsafeOperation",
]

ReportFormat = Literal["json", "csv"]
_MEMORY_SCANS = 16
_MEMORY_PLANS = 32
_CHILD_LIMIT = 15


class ScanNotFound(LookupError):
    pass


class ArtifactNotFound(LookupError):
    pass


class PlanNotFound(LookupError):
    pass


class PathNotAllowed(PermissionError):
    """The path is outside every configured scan root."""


class SiftService:
    def __init__(
        self,
        config: SiftConfig | None = None,
        *,
        roots: Sequence[str] = (),
        config_file: Path | None = None,
        history_file: Path | None = None,
    ) -> None:
        self._config_file = config_file
        self._history_file = history_file
        self._extra_roots = [scanner.resolve_root(root) for root in roots]
        self._lock = threading.Lock()
        self._scans: OrderedDict[str, ScanResult] = OrderedDict()
        self._plans: OrderedDict[str, CleanupPlan] = OrderedDict()
        self.config = config if config is not None else load_config(config_file)
        self.ruleset: RuleSet = build_ruleset(self.config.detectors)
        self.history: History | None = self._open_history()

    def _open_history(self) -> History | None:
        if not self.config.history.enabled:
            return None
        target = self._history_file or data_dir() / "history.db"
        return History(target, max_entries=self.config.history.max_entries)

    # --- configuration ------------------------------------------------------------------

    @property
    def roots(self) -> list[str]:
        """Scan roots the HTTP API may touch: from the config plus any given at start-up."""
        seen: dict[str, None] = {}
        for root in [*self._extra_roots, *self.config.resolved_roots()]:
            if os.path.isdir(root):
                seen.setdefault(os.path.realpath(root), None)
        return list(seen)

    def apply_config(self, config: SiftConfig, *, persist: bool = True) -> None:
        """Switch to a new configuration, optionally writing it to the config file."""
        if persist:
            save_config(config, self._config_file)
        self.config = config
        self.ruleset = build_ruleset(config.detectors)
        self.history = self._open_history()

    def resolve_allowed(self, path: str | os.PathLike[str]) -> str:
        """Resolve ``path`` and insist that it lies inside a configured scan root."""
        resolved = scanner.resolve_root(path)
        candidate = os.path.normcase(resolved)
        for root in self.roots:
            base = os.path.normcase(root)
            if candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep):
                return resolved
        raise PathNotAllowed(f"{resolved} is not inside a configured scan root")

    # --- scanning -----------------------------------------------------------------------

    def scan_options(
        self,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        exclude: Sequence[str] = (),
        min_confidence: Confidence | None = None,
        time_budget: float | None = None,
        measure: bool = True,
    ) -> ScanOptions:
        """Combine per-scan choices with the configured defaults."""
        scan = self.config.scan
        return ScanOptions(
            recursive=recursive,
            max_depth=max_depth if max_depth is not None else scan.max_depth,
            exclude=[*scan.exclude, *exclude],
            detectors=[detector.name for detector in self.ruleset.detectors],
            min_confidence=min_confidence or scan.min_confidence,
            measure=measure,
            time_budget_seconds=time_budget
            if time_budget is not None
            else scan.time_budget_seconds,
        )

    def scan(
        self,
        path: str | os.PathLike[str],
        options: ScanOptions | None = None,
        *,
        progress: ProgressCallback | None = None,
        record: bool = True,
    ) -> ScanResult:
        """Scan ``path``. Read-only apart from recording the result in local history."""
        result = scanner.scan(
            path,
            options or self.scan_options(),
            self.ruleset,
            workers=self.config.scan.workers,
            progress=progress,
        )
        self._remember(result)
        if record and self.history is not None:
            self.history.save_scan(result)
        return result

    def _remember(self, result: ScanResult) -> None:
        with self._lock:
            self._scans[result.id] = result
            self._scans.move_to_end(result.id)
            while len(self._scans) > _MEMORY_SCANS:
                self._scans.popitem(last=False)

    def get_scan(self, scan_id: str) -> ScanResult:
        with self._lock:
            found = self._scans.get(scan_id)
        if found is None and self.history is not None:
            found = self.history.get_scan(scan_id)
        if found is None:
            raise ScanNotFound(scan_id)
        return found

    def latest_scan(self) -> ScanResult | None:
        with self._lock:
            if self._scans:
                return next(reversed(self._scans.values()))
        return self.history.latest_scan() if self.history is not None else None

    def list_scans(self, limit: int = 50) -> list[ScanSummary]:
        if self.history is not None:
            return self.history.list_scans(limit)
        with self._lock:
            recent = list(reversed(self._scans.values()))[:limit]
        return [
            ScanSummary(
                id=scan.id,
                root=scan.root,
                started_at=scan.started_at,
                recursive=scan.options.recursive,
                artifact_count=scan.totals.artifact_count,
                logical_bytes=scan.totals.logical_bytes,
                allocated_bytes=scan.totals.allocated_bytes,
                total_ms=scan.stats.total_ms,
            )
            for scan in recent
        ]

    def export_scan(self, scan: ScanResult, fmt: ReportFormat) -> str:
        return reports.scan_to_json(scan) if fmt == "json" else reports.scan_to_csv(scan)

    # --- inspecting ---------------------------------------------------------------------

    def find_artifact(self, artifact_id: str, scan_id: str | None = None) -> ScanResult:
        """The scan that contains ``artifact_id`` - a specific one, or the most recent."""
        if scan_id is not None:
            scan = self.get_scan(scan_id)
            if scan.artifact(artifact_id) is None:
                raise ArtifactNotFound(artifact_id)
            return scan
        with self._lock:
            recent = list(reversed(self._scans.values()))
        for scan in recent:
            if scan.artifact(artifact_id) is not None:
                return scan
        if self.history is not None:
            stored = self.history.find_artifact(artifact_id)
            if stored is not None:
                return stored
        raise ArtifactNotFound(artifact_id)

    def check_cleanup(self, scan: ScanResult, artifact_id: str) -> CleanupItem:
        """Would this artifact be allowed in a cleanup right now? Read-only."""
        artifact = scan.artifact(artifact_id)
        if artifact is None:
            raise ArtifactNotFound(artifact_id)
        return cleanup.plan_item(artifact, scan.root, self.ruleset, scan.options.min_confidence)

    def inspect(self, artifact_id: str, scan_id: str | None = None) -> ArtifactDetail:
        """Re-examine an artifact on disk: does it still exist, what is in it, is it safe?"""
        scan = self.find_artifact(artifact_id, scan_id)
        artifact = scan.artifact(artifact_id)
        assert artifact is not None
        exists = os.path.lexists(sizing.extended_path(artifact.path))
        detection = self.ruleset.classify_path(artifact.path) if exists else None
        still_matches = (
            detection is not None
            and detection.rule.id == artifact.rule
            and detection.ambiguous == artifact.ambiguous
        )
        verdict = self.check_cleanup(scan, artifact_id)
        return ArtifactDetail(
            artifact=artifact,
            scan_id=scan.id,
            scan_root=scan.root,
            exists=exists,
            still_matches=still_matches,
            current_size=sizing.measure(artifact.path, artifact.kind) if exists else None,
            children=_children(artifact.path) if exists and artifact.kind == "directory" else [],
            cleanup_allowed=verdict.allowed,
            cleanup_refusal=verdict.refusal_reason,
        )

    # --- cleanup ------------------------------------------------------------------------

    def plan_cleanup(
        self, scan: ScanResult, artifact_ids: Sequence[str], *, dry_run: bool = False
    ) -> CleanupPlan:
        """Build the preview for an explicit selection. Deletes nothing."""
        plan = cleanup.build_plan(scan, artifact_ids, self.ruleset, dry_run=dry_run)
        with self._lock:
            self._plans[plan.id] = plan
            while len(self._plans) > _MEMORY_PLANS:
                self._plans.popitem(last=False)
        return plan

    def get_plan(self, plan_id: str) -> CleanupPlan:
        with self._lock:
            plan = self._plans.get(plan_id)
        if plan is None:
            raise PlanNotFound(plan_id)
        return plan

    def execute_cleanup(
        self,
        plan: CleanupPlan,
        confirmation_token: str | None = None,
        *,
        progress: Callable[[int, int, CleanupItem], None] | None = None,
    ) -> CleanupResult:
        """Run a previewed plan. A real run needs the plan's confirmation token."""
        result = cleanup.execute(plan, confirmation_token, self.ruleset, progress=progress)
        self._finish(plan, result)
        return result

    def cancel_cleanup(self, plan: CleanupPlan) -> CleanupResult:
        """The user backed out at the confirmation step. Nothing is deleted."""
        result = cleanup.cancel(plan)
        self._finish(plan, result)
        return result

    def _finish(self, plan: CleanupPlan, result: CleanupResult) -> None:
        with self._lock:
            self._plans.pop(plan.id, None)
        if self.history is not None:
            self.history.save_cleanup(result)

    def list_cleanups(self, limit: int = 50) -> list[CleanupSummary]:
        return self.history.list_cleanups(limit) if self.history is not None else []

    def get_cleanup(self, cleanup_id: str) -> CleanupResult | None:
        return self.history.get_cleanup(cleanup_id) if self.history is not None else None


def _children(path: str) -> list[ChildEntry]:
    """The largest immediate children of an artifact directory."""
    entries: list[ChildEntry] = []
    directories: list[str] = []
    try:
        with os.scandir(sizing.extended_path(path)) as listing:
            for entry in listing:
                try:
                    if is_link(entry):
                        entries.append(
                            ChildEntry(name=entry.name, kind="link", logical_bytes=0, file_count=0)
                        )
                    elif entry.is_dir(follow_symlinks=False):
                        directories.append(entry.name)
                    else:
                        size = entry.stat(follow_symlinks=False).st_size
                        entries.append(
                            ChildEntry(
                                name=entry.name, kind="file", logical_bytes=size, file_count=1
                            )
                        )
                except OSError:
                    continue
    except OSError:
        return []
    measured = sizing.measure_all([(os.path.join(path, name), "directory") for name in directories])
    for name in directories:
        info = measured.get(os.path.join(path, name))
        entries.append(
            ChildEntry(
                name=name,
                kind="directory",
                logical_bytes=info.logical_bytes if info else 0,
                file_count=info.file_count if info else 0,
            )
        )
    entries.sort(key=lambda child: (-child.logical_bytes, child.name))
    return entries[:_CHILD_LIMIT]
