"""Request and response bodies that exist only at the HTTP boundary.

Scan results, artifacts and cleanup plans are the core models from
:mod:`sift.core.models`, returned unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sift.core.config import SiftConfig
from sift.core.models import CleanupSummary, Confidence, ScanSummary


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(BaseModel):
    status: str = "ok"
    version: str
    roots: int
    """How many scan roots are configured. Zero means nothing can be scanned yet."""


class ConfigInfo(BaseModel):
    config_path: str
    config_exists: bool
    roots: list[str]
    """Scan roots in effect. Read-only here: they are changed in the config file or with
    ``sift serve --root``, never through the API."""
    available_detectors: list[str]
    config: SiftConfig


class SettingsUpdate(_Body):
    """The settings the dashboard may change. Scan roots are deliberately absent."""

    exclude: list[str] | None = None
    max_depth: int | None = Field(default=None, ge=0)
    min_confidence: Confidence | None = None
    time_budget_seconds: float | None = Field(default=None, gt=0)
    detectors_enabled: list[str] | None = None
    go_artifact_dirs: list[str] | None = None
    history_enabled: bool | None = None


class ScanRequest(_Body):
    path: str
    recursive: bool = False
    max_depth: int | None = Field(default=None, ge=0)


class CleanupPreviewRequest(_Body):
    scan_id: str
    artifact_ids: list[str] = Field(min_length=1)
    dry_run: bool = False


class CleanupExecuteRequest(_Body):
    plan_id: str
    confirmation_token: str = ""
    confirmation: str = ""
    """The word the user typed. A real cleanup needs both this and the token."""


class CleanupCancelRequest(_Body):
    plan_id: str


class HistoryResponse(BaseModel):
    enabled: bool
    scans: list[ScanSummary]
    cleanups: list[CleanupSummary]


class ErrorResponse(BaseModel):
    detail: str
    code: str
