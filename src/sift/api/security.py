"""Request checks that keep the local API local.

The API can delete files, so it must not be drivable by anything other than the dashboard
it serves. There is no login; instead every request is checked:

* **Host header** must name a loopback address. A website that points its own domain at
  127.0.0.1 (DNS rebinding) still sends its own name in ``Host``, so it is rejected.
* **State-changing requests** must not come from another site (``Origin`` and
  ``Sec-Fetch-Site`` are checked) and must be JSON. Browsers cannot send JSON across
  origins without a CORS preflight, and this server never answers one with permission.
* **Responses** forbid framing (no clickjacking) and are never cached.

No CORS headers are ever sent, so other origins cannot read responses either.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from sift.core.config import LOOPBACK_HOSTS

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


def _hostname(value: str) -> str:
    """Host part of a ``Host`` header or origin: ``[::1]:8765`` -> ``::1``."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if ":" in value else value


def is_loopback_host(header: str | None) -> bool:
    return bool(header) and _hostname(header or "") in LOOPBACK_HOSTS


def is_loopback_origin(origin: str) -> bool:
    parts = urlsplit(origin)
    return parts.scheme in {"http", "https"} and (parts.hostname or "") in LOOPBACK_HOSTS


def _reject(status: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not is_loopback_host(request.headers.get("host")):
            return _reject(400, "bad-host", "this server only answers to loopback host names")

        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not is_loopback_origin(origin):
                return _reject(403, "cross-origin", "requests from other sites are not accepted")
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return _reject(403, "cross-origin", "requests from other sites are not accepted")
            content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                return _reject(415, "not-json", "request bodies must be application/json")

        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = _CSP
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response
