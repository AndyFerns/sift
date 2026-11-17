"""The FastAPI application.

Every route is a thin wrapper around :class:`~sift.core.service.SiftService`. The API adds
only what HTTP needs: request validation, scan-root enforcement and error mapping.

Filesystem access is limited to the configured scan roots. A scan may only be started
inside a root, and a stored scan can only be read, inspected or cleaned through the API
if its root is still inside one.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from sift import __version__
from sift.api.schemas import (
    CleanupCancelRequest,
    CleanupExecuteRequest,
    CleanupPreviewRequest,
    ConfigInfo,
    ErrorResponse,
    Health,
    HistoryResponse,
    ScanRequest,
    SettingsUpdate,
)
from sift.api.security import LocalOnlyMiddleware
from sift.core.cleanup import CONFIRMATION_WORD, UnknownArtifact
from sift.core.config import BUILTIN_DETECTORS, ConfigError, SiftConfig, config_path
from sift.core.models import ArtifactDetail, CleanupPlan, CleanupResult, ScanResult, ScanSummary
from sift.core.service import (
    ArtifactNotFound,
    PathNotAllowed,
    PlanNotFound,
    ScanError,
    ScanNotFound,
    SiftService,
    UnsafeOperation,
)

STATIC_DIR = Path(__file__).parent / "static"

_NOT_BUILT = """<!doctype html>
<meta charset="utf-8">
<title>Sift</title>
<body style="font-family: system-ui, sans-serif; max-width: 40rem; margin: 4rem auto">
<h1>Sift API is running</h1>
<p>The dashboard has not been built, so there is nothing to show here yet.</p>
<p>From a source checkout: <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>,
then restart <code>sift serve</code>.</p>
<p>The API itself is available: <a href="/api/docs">interactive API documentation</a>.</p>
</body>
"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


_ERRORS: dict[int | str, dict[str, object]] = {
    400: {"model": ErrorResponse},
    403: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
    422: {"model": ErrorResponse},
}


def _inside(root: str, path: str) -> bool:
    base = os.path.normcase(root)
    candidate = os.path.normcase(path)
    return candidate == base or candidate.startswith(base.rstrip(os.sep) + os.sep)


def create_app(service: SiftService, *, static_dir: Path | None = None) -> FastAPI:
    """Build the API for ``service``. ``static_dir`` holds the built dashboard, if any."""
    app = FastAPI(
        title="Sift",
        version=__version__,
        description="Local API for discovering and safely cleaning development artifacts.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(LocalOnlyMiddleware)
    api = APIRouter(prefix="/api", responses=_ERRORS)

    @app.exception_handler(ApiError)
    def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    def _invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"] if part != "body")
        detail = f"{where}: {first['msg']}" if where else str(first["msg"])
        return JSONResponse({"detail": detail, "code": "invalid-request"}, status_code=422)

    @app.exception_handler(HTTPException)
    def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc.detail), "code": "http-error"}, status_code=exc.status_code
        )

    # --- helpers ------------------------------------------------------------------------

    def allowed_scan(scan_id: str) -> ScanResult:
        """A stored scan, provided its root is still one the API may touch."""
        try:
            scan = service.get_scan(scan_id)
        except ScanNotFound:
            raise ApiError(404, "scan-not-found", f"no scan with id {scan_id}") from None
        if not any(_inside(root, scan.root) for root in service.roots):
            raise ApiError(403, "outside-roots", "that scan is outside the configured scan roots")
        return scan

    def config_info() -> ConfigInfo:
        target = config_path()
        return ConfigInfo(
            config_path=str(target),
            config_exists=target.exists(),
            roots=service.roots,
            available_detectors=list(BUILTIN_DETECTORS),
            config=service.config,
        )

    # --- health and configuration -------------------------------------------------------

    @api.get("/health", response_model=Health)
    def health() -> Health:
        return Health(version=__version__, roots=len(service.roots))

    @api.get("/config", response_model=ConfigInfo)
    def get_config() -> ConfigInfo:
        return config_info()

    @api.put("/config", response_model=ConfigInfo)
    def update_config(update: SettingsUpdate) -> ConfigInfo:
        """Change scan settings. Scan roots cannot be changed through the API."""
        data = service.config.model_dump(mode="json")
        changed = update.model_fields_set
        if "exclude" in changed and update.exclude is not None:
            data["scan"]["exclude"] = update.exclude
        if "max_depth" in changed:
            data["scan"]["max_depth"] = update.max_depth
        if "min_confidence" in changed and update.min_confidence is not None:
            data["scan"]["min_confidence"] = update.min_confidence.value
        if "time_budget_seconds" in changed:
            data["scan"]["time_budget_seconds"] = update.time_budget_seconds
        if "detectors_enabled" in changed and update.detectors_enabled is not None:
            data["detectors"]["enabled"] = update.detectors_enabled
        if "go_artifact_dirs" in changed and update.go_artifact_dirs is not None:
            data["detectors"]["go"]["artifact_dirs"] = update.go_artifact_dirs
        if "history_enabled" in changed and update.history_enabled is not None:
            data["history"]["enabled"] = update.history_enabled
        try:
            config = SiftConfig.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(part) for part in first["loc"])
            raise ApiError(400, "invalid-settings", f"{where}: {first['msg']}") from exc
        try:
            service.apply_config(config)
        except (OSError, ConfigError) as exc:
            raise ApiError(400, "config-not-saved", f"could not save settings: {exc}") from exc
        return config_info()

    # --- scans --------------------------------------------------------------------------

    @api.post("/scans", response_model=ScanResult)
    def start_scan(body: ScanRequest) -> ScanResult:
        """Scan a directory inside a configured scan root. Read-only."""
        if not service.roots:
            raise ApiError(
                403,
                "no-roots",
                "no scan roots are configured; add some to config.toml "
                "or start with `sift serve --root PATH`",
            )
        try:
            path = service.resolve_allowed(body.path)
        except PathNotAllowed:
            raise ApiError(
                403, "outside-roots", "that path is outside the configured scan roots"
            ) from None
        except ScanError as exc:
            raise ApiError(404, "path-not-found", str(exc)) from exc
        options = service.scan_options(recursive=body.recursive, max_depth=body.max_depth)
        try:
            return service.scan(path, options)
        except ScanError as exc:
            raise ApiError(404, "path-not-found", str(exc)) from exc

    @api.get("/scans", response_model=list[ScanSummary])
    def list_scans(limit: int = Query(50, ge=1, le=500)) -> list[ScanSummary]:
        roots = service.roots
        return [
            summary
            for summary in service.list_scans(limit)
            if any(_inside(root, summary.root) for root in roots)
        ]

    @api.get("/scans/latest", response_model=ScanResult)
    def latest_scan() -> ScanResult:
        """The most recent scan inside the configured roots."""
        for summary in list_scans(limit=500):
            return allowed_scan(summary.id)
        raise ApiError(404, "scan-not-found", "nothing has been scanned yet")

    @api.get("/scans/{scan_id}", response_model=ScanResult)
    def get_scan(scan_id: str) -> ScanResult:
        return allowed_scan(scan_id)

    @api.get("/scans/{scan_id}/artifacts/{artifact_id}", response_model=ArtifactDetail)
    def get_artifact(scan_id: str, artifact_id: str) -> ArtifactDetail:
        """One artifact, re-examined on disk right now."""
        allowed_scan(scan_id)
        try:
            return service.inspect(artifact_id, scan_id)
        except ArtifactNotFound:
            raise ApiError(
                404, "artifact-not-found", f"no artifact {artifact_id} in that scan"
            ) from None

    @api.get("/scans/{scan_id}/report")
    def export_report(
        scan_id: str, fmt: Literal["json", "csv"] = Query("json", alias="format")
    ) -> Response:
        """Download a scan as JSON or CSV."""
        scan = allowed_scan(scan_id)
        media = "application/json" if fmt == "json" else "text/csv; charset=utf-8"
        return Response(
            service.export_scan(scan, fmt),
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="sift-{scan.id}.{fmt}"'},
        )

    # --- cleanup ------------------------------------------------------------------------

    @api.post("/cleanup/preview", response_model=CleanupPlan)
    def preview_cleanup(body: CleanupPreviewRequest) -> CleanupPlan:
        """Build a cleanup preview for an explicit selection. Deletes nothing."""
        scan = allowed_scan(body.scan_id)
        try:
            return service.plan_cleanup(scan, body.artifact_ids, dry_run=body.dry_run)
        except UnknownArtifact as exc:
            raise ApiError(404, "artifact-not-found", str(exc)) from exc

    def find_plan(plan_id: str) -> CleanupPlan:
        try:
            return service.get_plan(plan_id)
        except PlanNotFound:
            raise ApiError(
                404, "plan-not-found", "that preview no longer exists; build a new one"
            ) from None

    @api.post("/cleanup/execute", response_model=CleanupResult)
    def execute_cleanup(body: CleanupExecuteRequest) -> CleanupResult:
        """Run a previewed cleanup.

        A dry run needs nothing more. A real run needs the preview's confirmation token
        *and* the typed confirmation word; every item is validated again before deletion.
        """
        plan = find_plan(body.plan_id)
        if not plan.dry_run and body.confirmation.strip().lower() != CONFIRMATION_WORD:
            raise ApiError(
                409,
                "not-confirmed",
                f'cleanup was not confirmed: type "{CONFIRMATION_WORD}" to proceed',
            )
        try:
            return service.execute_cleanup(plan, body.confirmation_token)
        except UnsafeOperation as exc:
            raise ApiError(409, exc.code, exc.reason) from exc

    @api.post("/cleanup/cancel", response_model=CleanupResult)
    def cancel_cleanup(body: CleanupCancelRequest) -> CleanupResult:
        """Record that the user backed out of a preview. Deletes nothing."""
        return service.cancel_cleanup(find_plan(body.plan_id))

    @api.get("/cleanups/{cleanup_id}", response_model=CleanupResult)
    def get_cleanup(cleanup_id: str) -> CleanupResult:
        result = service.get_cleanup(cleanup_id)
        if result is None or not any(_inside(root, result.root) for root in service.roots):
            raise ApiError(404, "cleanup-not-found", f"no cleanup with id {cleanup_id}")
        return result

    @api.get("/history", response_model=HistoryResponse)
    def history(limit: int = Query(50, ge=1, le=500)) -> HistoryResponse:
        roots = service.roots
        return HistoryResponse(
            enabled=service.history is not None,
            scans=list_scans(limit=limit),
            cleanups=[
                summary
                for summary in service.list_cleanups(limit)
                if any(_inside(root, summary.root) for root in roots)
            ],
        )

    @api.api_route(
        "/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    def unknown_api_route(rest: str) -> Response:
        raise ApiError(404, "not-found", f"no such endpoint: /api/{rest}")

    app.include_router(api)

    # --- dashboard ----------------------------------------------------------------------

    static = static_dir if static_dir is not None else STATIC_DIR
    index = static / "index.html"
    if index.is_file():
        assets = static / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{rest:path}", include_in_schema=False)
        def dashboard(rest: str) -> Response:
            # Top-level static files (favicon and friends), otherwise the single-page app.
            candidate = static / rest
            if rest and "/" not in rest and "\\" not in rest and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index, media_type="text/html")

    else:

        @app.get("/", include_in_schema=False)
        def not_built() -> HTMLResponse:
            return HTMLResponse(_NOT_BUILT)

    return app
