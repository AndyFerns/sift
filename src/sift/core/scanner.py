"""The scanner: find artifacts, then measure them.

Scanning is strictly read-only and happens in two phases.

**Discovery** walks the tree from the scan root with ``os.scandir``. Whenever it meets an
entry whose name a detector cares about, the detector decides whether it is an artifact.
The walk never descends into an artifact, never follows a symlink or junction, and never
enters version-control metadata, so it only ever reads source directories. This is what
keeps discovery fast even when the artifacts themselves hold millions of files.

**Sizing** then measures each artifact in parallel (see :mod:`sift.core.sizing`).

Scan modes:

* default - the root is treated as one project. Nested repositories (directories below
  the root that contain ``.git``) belong to someone else and are reported but not entered.
* recursive - every project under the root is scanned, nested repositories included.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sift.core import sizing
from sift.core.detectors import PROJECT_MANIFESTS, Candidate, Detection, RuleSet, list_dir
from sift.core.ignore import (
    IGNORE_FILE,
    IgnoreLayer,
    is_ignored,
    layer_from_patterns,
    load_ignore_file,
)
from sift.core.models import (
    Artifact,
    Ecosystem,
    Project,
    ScanIssue,
    ScanOptions,
    ScanResult,
    ScanStats,
    ScanTotals,
    SizeInfo,
)

# Version-control metadata is never an artifact and never worth walking.
_NEVER_ENTER = frozenset({".git", ".hg", ".svn"})
_PROGRESS_EVERY = 200


class ScanError(Exception):
    """The scan could not start (for example the root does not exist)."""


@dataclass(slots=True)
class ScanProgress:
    phase: str  # "discovery", "sizing" or "done"
    directories_visited: int = 0
    artifacts_found: int = 0
    artifacts_measured: int = 0
    current: str = ""


ProgressCallback = Callable[[ScanProgress], None]


@dataclass(slots=True)
class _ProjectState:
    path: str
    is_repository: bool
    manifests: list[str]
    artifact_ids: list[str] = field(default_factory=list)


def artifact_id(path: str) -> str:
    """Stable identifier for an artifact: a short hash of its normalised absolute path."""
    normalised = os.path.normcase(os.path.normpath(path))
    digest = hashlib.blake2b(normalised.encode("utf-8", "surrogatepass"), digest_size=6)
    return digest.hexdigest()


def new_scan_id() -> str:
    return f"{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"


def resolve_root(path: str | os.PathLike[str]) -> str:
    """Absolute, link-free form of a scan root. Raises ScanError if it is not a directory."""
    expanded = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if not os.path.exists(expanded):
        raise ScanError(f"{expanded} does not exist")
    if not os.path.isdir(expanded):
        raise ScanError(f"{expanded} is not a directory")
    return os.path.realpath(expanded)


def _relative(path: str, root: str) -> str:
    relative = os.path.relpath(path, root).replace(os.sep, "/")
    return "." if relative == "." else relative


class _Discovery:
    def __init__(
        self,
        root: str,
        options: ScanOptions,
        ruleset: RuleSet,
        progress: ProgressCallback | None,
    ) -> None:
        self.root = root
        self.options = options
        self.ruleset = ruleset
        self.progress = progress
        self.projects: list[_ProjectState] = []
        self.artifacts: list[Artifact] = []
        self.issues: list[ScanIssue] = []
        self.visited = 0
        self.links_skipped = 0
        self.excluded = 0

    def run(self) -> None:
        layers: tuple[IgnoreLayer, ...] = ()
        configured = layer_from_patterns(self.root, self.options.exclude)
        if configured is not None:
            layers = (configured,)

        # (directory, depth, index of owning project, active ignore layers)
        stack: list[tuple[str, int, int, tuple[IgnoreLayer, ...]]] = [(self.root, 0, -1, layers)]
        max_depth = self.options.max_depth
        dir_rules = self.ruleset.dir_rules
        file_rules = self.ruleset.file_rules

        while stack:
            path, depth, owner, layers = stack.pop()
            try:
                listing = list_dir(path)
            except PermissionError as exc:
                self._issue("permission", path, f"permission denied: {exc.strerror or exc}")
                continue
            except (FileNotFoundError, NotADirectoryError):
                self._issue("vanished", path, "disappeared while scanning")
                continue
            except OSError as exc:
                self._issue("error", path, f"could not be read: {exc.strerror or exc}")
                continue

            is_repository = ".git" in listing.dirs or ".git" in listing.files
            if depth > 0 and is_repository and not self.options.recursive:
                self._issue(
                    "nested-repository",
                    path,
                    "nested repository skipped; use a recursive scan to include it",
                )
                continue

            self.visited += 1
            self.links_skipped += len(listing.links)
            if self.progress is not None and self.visited % _PROGRESS_EVERY == 0:
                self.progress(
                    ScanProgress("discovery", self.visited, len(self.artifacts), current=path)
                )

            manifests = [name for name in PROJECT_MANIFESTS if name in listing.files]
            if depth == 0 or is_repository or manifests:
                self.projects.append(_ProjectState(path, is_repository, manifests))
                owner = len(self.projects) - 1

            if IGNORE_FILE in listing.files:
                extra = load_ignore_file(path)
                if extra is not None:
                    layers = (*layers, extra)

            if file_rules:
                for name in listing.files:
                    if name not in file_rules:
                        continue
                    child = os.path.join(path, name)
                    if layers and is_ignored(layers, child, False):
                        self.excluded += 1
                        continue
                    detection = self.ruleset.classify(Candidate(child, name, False, listing))
                    if detection is not None:
                        self._record(detection, owner)

            for name in listing.dirs:
                if name in _NEVER_ENTER:
                    continue
                child = os.path.join(path, name)
                if layers and is_ignored(layers, child, True):
                    self.excluded += 1
                    continue
                if name in dir_rules:
                    detection = self.ruleset.classify(Candidate(child, name, True, listing))
                    if detection is not None:
                        # An artifact is opaque: nothing inside it is examined.
                        self._record(detection, owner)
                        continue
                if max_depth is None or depth < max_depth:
                    stack.append((child, depth + 1, owner, layers))

    def _issue(self, kind: str, path: str, message: str) -> None:
        self.issues.append(ScanIssue(kind=kind, path=path, message=message))

    def _record(self, detection: Detection, owner: int) -> None:
        rule = detection.rule
        project = self.projects[owner]
        identifier = artifact_id(detection.path)
        cleanable = not detection.ambiguous and detection.confidence.meets(
            self.options.min_confidence
        )
        review_reason = detection.review_reason
        if not cleanable and review_reason is None:
            review_reason = (
                f"confidence is {detection.confidence.value}, below the "
                f"{self.options.min_confidence.value} threshold"
            )
        self.artifacts.append(
            Artifact(
                id=identifier,
                name=os.path.basename(detection.path),
                path=detection.path,
                relative_path=_relative(detection.path, self.root),
                kind=rule.kind,
                ecosystem=rule.ecosystem,
                rule=rule.id,
                category=rule.category,
                description=rule.description,
                confidence=detection.confidence,
                ambiguous=detection.ambiguous,
                cleanable=cleanable,
                review_reason=review_reason,
                evidence=detection.evidence,
                project_path=project.path,
            )
        )
        project.artifact_ids.append(identifier)


def _sum_optional(values: list[int | None]) -> int | None:
    if not values or any(value is None for value in values):
        return None
    return sum(value for value in values if value is not None)


def _totals(artifacts: list[Artifact]) -> ScanTotals:
    measured = [a for a in artifacts if a.size is not None]
    sizes = [a.size for a in measured if a.size is not None]
    cleanable = [a.size for a in measured if a.cleanable and a.size is not None]
    return ScanTotals(
        artifact_count=len(artifacts),
        cleanable_count=sum(1 for a in artifacts if a.cleanable),
        review_count=sum(1 for a in artifacts if not a.cleanable),
        measured_count=len(measured),
        file_count=sum(s.file_count for s in sizes),
        logical_bytes=sum(s.logical_bytes for s in sizes),
        allocated_bytes=_sum_optional([s.allocated_bytes for s in sizes]),
        cleanable_logical_bytes=sum(s.logical_bytes for s in cleanable),
        cleanable_allocated_bytes=_sum_optional([s.allocated_bytes for s in cleanable]),
    )


def _build_projects(
    states: list[_ProjectState], artifacts: dict[str, Artifact], root: str
) -> list[Project]:
    projects: list[Project] = []
    for state in states:
        if not state.artifact_ids:
            continue
        owned = [artifacts[i] for i in state.artifact_ids]
        ecosystems: dict[Ecosystem, None] = {}
        for name in state.manifests:
            ecosystems.setdefault(PROJECT_MANIFESTS[name], None)
        for artifact in owned:
            if not artifact.ambiguous:
                ecosystems.setdefault(artifact.ecosystem, None)
        sizes = [a.size for a in owned if a.size is not None]
        projects.append(
            Project(
                path=state.path,
                relative_path=_relative(state.path, root),
                name=os.path.basename(state.path) or state.path,
                is_repository=state.is_repository,
                manifests=state.manifests,
                ecosystems=list(ecosystems),
                artifact_ids=[a.id for a in sorted(owned, key=_by_size)],
                logical_bytes=sum(s.logical_bytes for s in sizes),
                allocated_bytes=_sum_optional([s.allocated_bytes for s in sizes]),
            )
        )
    projects.sort(key=lambda p: (-p.logical_bytes, p.path))
    return projects


def _by_size(artifact: Artifact) -> tuple[int, str]:
    size = artifact.size.logical_bytes if artifact.size is not None else -1
    return (-size, artifact.path)


def scan(
    root: str | os.PathLike[str],
    options: ScanOptions | None = None,
    ruleset: RuleSet | None = None,
    *,
    workers: int | None = None,
    progress: ProgressCallback | None = None,
) -> ScanResult:
    """Scan ``root`` and return every artifact found, largest first. Read-only."""
    from sift.core.detectors import build_ruleset

    options = options or ScanOptions()
    ruleset = ruleset or build_ruleset()
    resolved = resolve_root(root)
    started_at = datetime.now(UTC)
    clock = time.perf_counter()

    discovery = _Discovery(resolved, options, ruleset, progress)
    discovery.run()
    discovered = time.perf_counter()

    artifacts = discovery.artifacts
    issues = discovery.issues
    if options.measure and artifacts:
        measured_count = 0

        def on_measured(_path: str, _info: SizeInfo | None) -> None:
            nonlocal measured_count
            measured_count += 1
            if progress is not None:
                progress(
                    ScanProgress("sizing", discovery.visited, len(artifacts), measured_count, _path)
                )

        sizes = sizing.measure_all(
            [(a.path, a.kind) for a in artifacts],
            workers=workers,
            time_budget=options.time_budget_seconds,
            on_measured=on_measured,
        )
        for artifact in artifacts:
            artifact.size = sizes.get(artifact.path)
            if artifact.size is None:
                issues.append(
                    ScanIssue(
                        kind="not-measured",
                        path=artifact.path,
                        message="not measured: the time budget ran out",
                    )
                )
            elif not os.path.lexists(artifact.path):
                issues.append(
                    ScanIssue(
                        kind="vanished",
                        path=artifact.path,
                        message="disappeared before it could be measured",
                    )
                )
            elif not artifact.size.complete:
                issues.append(
                    ScanIssue(
                        kind="error",
                        path=artifact.path,
                        message=(
                            f"{artifact.size.error_count} entries could not be read; "
                            "the reported size is a lower bound"
                        ),
                    )
                )
    finished = time.perf_counter()

    artifacts.sort(key=_by_size)
    by_id = {a.id: a for a in artifacts}
    result = ScanResult(
        id=new_scan_id(),
        root=resolved,
        started_at=started_at,
        options=options,
        projects=_build_projects(discovery.projects, by_id, resolved),
        artifacts=artifacts,
        issues=issues,
        totals=_totals(artifacts),
        stats=ScanStats(
            directories_visited=discovery.visited,
            links_skipped=discovery.links_skipped,
            excluded=discovery.excluded,
            discovery_ms=round((discovered - clock) * 1000, 2),
            sizing_ms=round((finished - discovered) * 1000, 2),
            total_ms=round((finished - clock) * 1000, 2),
        ),
    )
    if progress is not None:
        progress(
            ScanProgress("done", discovery.visited, len(artifacts), result.totals.measured_count)
        )
    return result
