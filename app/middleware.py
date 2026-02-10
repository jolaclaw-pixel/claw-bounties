"""Middleware module — CSRF, request ID, security headers, request logging."""
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.constants import ALLOWED_ORIGINS, CSRF_PROTECTED_PATHS, ERR_CSRF_FAILED, HONEYPOT_PATHS

logger = logging.getLogger(__name__)


def register_middleware(app: FastAPI) -> None:
    """Register all HTTP middleware on the FastAPI app.

    Args:
        app: The FastAPI application instance.
    """

    @app.middleware("http")
    async def block_scanners(request: Request, call_next: Any) -> Any:
        """Return 404 for common scanner/bot paths."""
        if request.url.path in HONEYPOT_PATHS:
            return JSONResponse(status_code=404, content={"detail": "Not found"})
        return await call_next(request)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next: Any) -> Any:
        """Add security headers including CSP to all responses."""
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https:; "
            "connect-src 'self' https://acpx.virtuals.io; "
            "frame-ancestors 'none'"
        )
        return response

    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Any) -> Any:
        """Attach a unique request ID to every request and response."""
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.middleware("http")
    async def request_logging(request: Request, call_next: Any) -> Any:
        """Log method, path, status, and duration for every request."""
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        request_id = getattr(request.state, "request_id", "")
        logger.info(
            "[%s] %s %s %s %.1fms",
            request_id, request.method, request.url.path, response.status_code, duration_ms,
        )
        return response

    @app.middleware("http")
    async def csrf_protection(request: Request, call_next: Any) -> Any:
        """Check Origin/Referer on POST requests to web form endpoints for CSRF protection."""
        if request.method == "POST":
            path = request.url.path
            is_web_form = path in CSRF_PROTECTED_PATHS or (
                path.startswith("/bounties/") and (path.endswith("/claim") or path.endswith("/fulfill"))
            )
            if is_web_form:
                origin = request.headers.get("origin")
                referer = request.headers.get("referer")
                origin_ok = origin in ALLOWED_ORIGINS if origin else False
                referer_ok = any(referer and referer.startswith(o) for o in ALLOWED_ORIGINS) if referer else False
                if not origin_ok and not referer_ok:
                    if origin or referer:
                        request_id = getattr(request.state, "request_id", "")
                        return JSONResponse(
                            status_code=403,
                            content={"detail": "CSRF validation failed", "code": ERR_CSRF_FAILED, "request_id": request_id},
                        )
        return await call_next(request)

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next: Any) -> Any:
        """Optional API key auth for write endpoints on /api/v1/ routes."""
        api_write_key = os.getenv("API_WRITE_KEY", "")
        if api_write_key and request.method in ("POST", "PUT", "DELETE"):
            if request.url.path.startswith("/api/v1/"):
                provided_key = request.headers.get("X-API-Key", "")
                if provided_key != api_write_key:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Invalid or missing API key"},
                    )
        return await call_next(request)
