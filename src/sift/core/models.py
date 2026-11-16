"""Data models shared by every Sift interface.

These are plain Pydantic models with no behaviour beyond small derived helpers.
The CLI, TUI and HTTP API all serialise exactly these objects, which is what keeps
the interfaces consistent with each other.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Ecosystem(StrEnum):
    RUST = "rust"
    NODE = "node"
    PYTHON = "python"
    JAVA = "java"
    CPP = "cpp"
    GO = "go"
    GENERAL = "general"
    CUSTOM = "custom"


class Confidence(StrEnum):
    """How strongly the evidence supports a classification."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return _CONFIDENCE_RANK[self]

    def meets(self, threshold: Confidence) -> bool:
        return self.rank >= threshold.rank


_CONFIDENCE_RANK = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}


class SizeMethod(StrEnum):
    """How the allocated size was obtained."""

    EXACT = "exact"  # reported by the filesystem (st_blocks)
    ESTIMATED = "estimated"  # file sizes rounded up to the volume cluster size
    UNAVAILABLE = "unavailable"


class Evidence(BaseModel):
    """One reason a detector gave for (or against) a classification."""

    kind: Literal["manifest", "marker", "name", "conflict"]
    detail: str
    path: str | None = None


class SizeInfo(BaseModel):
    """Disk usage of one artifact.

    ``logical_bytes`` is the sum of file lengths - what the files contain.
    ``allocated_bytes`` is the space those files occupy on disk, which is usually larger
    (files are stored in whole blocks) and occasionally smaller (sparse or compressed files).
    Allocated size is the better predictor of how much space deletion frees.
    """

    logical_bytes: int = 0
    allocated_bytes: int | None = None
    allocated_method: SizeMethod = SizeMethod.UNAVAILABLE
    file_count: int = 0
    dir_count: int = 0
    link_count: int = 0
    error_count: int = 0
    complete: bool = True
    """False when some entries could not be read, so the totals are a lower bound."""


class Artifact(BaseModel):
    id: str
    name: str
    path: str
    relative_path: str
    kind: Literal["directory", "file"] = "directory"
    ecosystem: Ecosystem
    rule: str
    category: str
    description: str
    confidence: Confidence
    ambiguous: bool = False
    cleanable: bool = False
    """True only when the artifact is unambiguous and meets the confidence threshold."""
    review_reason: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    project_path: str
    size: SizeInfo | None = None
    """None means the artifact was not measured (sizing disabled or time budget exhausted)."""


class Project(BaseModel):
    path: str
    relative_path: str
    name: str
    is_repository: bool = False
    manifests: list[str] = Field(default_factory=list)
    ecosystems: list[Ecosystem] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    logical_bytes: int = 0
    allocated_bytes: int | None = None


class ScanIssue(BaseModel):
    """Something the scanner could not read or deliberately skipped."""

    kind: Literal["permission", "vanished", "error", "nested-repository", "not-measured"]
    path: str
    message: str


class ScanOptions(BaseModel):
    recursive: bool = False
    max_depth: int | None = None
    exclude: list[str] = Field(default_factory=list)
    detectors: list[str] = Field(default_factory=list)
    min_confidence: Confidence = Confidence.MEDIUM
    measure: bool = True
    time_budget_seconds: float | None = None


class ScanTotals(BaseModel):
    artifact_count: int = 0
    cleanable_count: int = 0
    review_count: int = 0
    measured_count: int = 0
    file_count: int = 0
    logical_bytes: int = 0
    allocated_bytes: int | None = None
    cleanable_logical_bytes: int = 0
    cleanable_allocated_bytes: int | None = None


class ScanStats(BaseModel):
    directories_visited: int = 0
    links_skipped: int = 0
    excluded: int = 0
    discovery_ms: float = 0.0
    sizing_ms: float = 0.0
    total_ms: float = 0.0


class ScanResult(BaseModel):
    id: str
    root: str
    started_at: datetime
    options: ScanOptions
    projects: list[Project] = Field(default_factory=list)
    artifacts: list[Artifact] = Field(default_factory=list)
    issues: list[ScanIssue] = Field(default_factory=list)
    totals: ScanTotals = Field(default_factory=ScanTotals)
    stats: ScanStats = Field(default_factory=ScanStats)

    def artifact(self, artifact_id: str) -> Artifact | None:
        for artifact in self.artifacts:
            if artifact.id == artifact_id:
                return artifact
        return None


class ScanSummary(BaseModel):
    """A history row: enough to list past scans without loading their artifacts."""

    id: str
    root: str
    started_at: datetime
    recursive: bool
    artifact_count: int
    logical_bytes: int
    allocated_bytes: int | None
    total_ms: float


class ChildEntry(BaseModel):
    name: str
    kind: Literal["directory", "file", "link"]
    logical_bytes: int
    file_count: int


class ArtifactDetail(BaseModel):
    """An artifact re-examined on disk right now, as shown by ``sift inspect``."""

    artifact: Artifact
    scan_id: str
    scan_root: str
    exists: bool
    still_matches: bool
    """Whether the detector still classifies the path the same way."""
    current_size: SizeInfo | None = None
    children: list[ChildEntry] = Field(default_factory=list)
    cleanup_allowed: bool = False
    cleanup_refusal: str | None = None


# --- cleanup ---------------------------------------------------------------------------------


class CleanupStatus(StrEnum):
    DELETED = "deleted"
    PARTIAL = "partial"
    FAILED = "failed"
    REFUSED = "refused"
    DRY_RUN = "dry-run"
    CANCELLED = "cancelled"


class CleanupItem(BaseModel):
    """One selected artifact inside a cleanup plan."""

    artifact_id: str
    name: str
    path: str
    relative_path: str
    kind: Literal["directory", "file"]
    ecosystem: Ecosystem
    rule: str
    confidence: Confidence
    file_count: int = 0
    logical_bytes: int = 0
    allocated_bytes: int | None = None
    allowed: bool
    refusal_code: str | None = None
    refusal_reason: str | None = None
    identity: tuple[int, int] | None = None
    """(device, inode) captured at preview time and re-checked right before deletion."""


class CleanupPlanTotals(BaseModel):
    detected_count: int = 0
    detected_bytes: int = 0
    selected_count: int = 0
    selected_bytes: int = 0
    deletable_count: int = 0
    deletable_bytes: int = 0
    deletable_file_count: int = 0
    refused_count: int = 0
    refused_bytes: int = 0


class CleanupPlan(BaseModel):
    """A preview of exactly what a cleanup would delete. Nothing is deleted by creating one."""

    model_config = ConfigDict(frozen=False)

    id: str
    scan_id: str
    root: str
    created_at: datetime
    expires_at: datetime
    dry_run: bool = False
    min_confidence: Confidence = Confidence.MEDIUM
    items: list[CleanupItem] = Field(default_factory=list)
    totals: CleanupPlanTotals = Field(default_factory=CleanupPlanTotals)
    confirmation_token: str = ""
    """Must be passed back to execute a real (non dry-run) cleanup of exactly this plan."""

    @property
    def deletable(self) -> list[CleanupItem]:
        return [item for item in self.items if item.allowed]

    @property
    def refused(self) -> list[CleanupItem]:
        return [item for item in self.items if not item.allowed]


class CleanupItemResult(BaseModel):
    artifact_id: str
    name: str
    path: str
    relative_path: str
    status: CleanupStatus
    selected_bytes: int = 0
    deleted_bytes: int = 0
    failed_bytes: int = 0
    deleted_files: int = 0
    message: str | None = None
    errors: list[str] = Field(default_factory=list)


class CleanupTotals(BaseModel):
    """The four sizes are deliberately separate; they are never the same number by accident."""

    detected_bytes: int = 0
    selected_bytes: int = 0
    deleted_bytes: int = 0
    failed_bytes: int = 0
    refused_bytes: int = 0
    would_delete_bytes: int = 0
    """Dry runs only: what a real run would have attempted to delete."""
    deleted_count: int = 0
    failed_count: int = 0
    refused_count: int = 0


class CleanupResult(BaseModel):
    """Audit record of a cleanup attempt, including dry runs and cancellations."""

    id: str
    plan_id: str
    scan_id: str
    root: str
    started_at: datetime
    finished_at: datetime
    dry_run: bool
    cancelled: bool = False
    items: list[CleanupItemResult] = Field(default_factory=list)
    totals: CleanupTotals = Field(default_factory=CleanupTotals)


class CleanupSummary(BaseModel):
    id: str
    scan_id: str
    root: str
    started_at: datetime
    dry_run: bool
    cancelled: bool
    selected_bytes: int
    deleted_bytes: int
    failed_bytes: int
    item_count: int
