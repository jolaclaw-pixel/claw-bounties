"""Claw Bounties — application setup, middleware, lifespan, and router mounting."""
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv  # noqa: F401
from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.constants import (
    ALLOWED_ORIGINS,
    APP_VERSION,
    CSRF_PROTECTED_PATHS,
    ERR_CSRF_FAILED,
    ERR_INTERNAL,
    HONEYPOT_PATHS,
)
from app.database import init_db
from app.routers import bounties, misc, services
from app.routers.api_v1 import router as api_v1_router
from app.routers.web import router as web_router, templates

load_dotenv()

# ---- Structured JSON logging ----


class JSONFormatter(logging.Formatter):
    """JSON log formatter for structured logging in production."""

    def format(self, record: logging.LogRecord) -> str:
        """Format a log record as a JSON string."""
        log_entry: dict[str, Any] = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])
        return json.dumps(log_entry)


def _configure_logging() -> None:
    """Configure logging — JSON in production, plain in dev."""
    log_format = os.getenv("LOG_FORMAT", "json" if os.getenv("RAILWAY_ENVIRONMENT") else "text")
    handler = logging.StreamHandler(sys.stdout)

    if log_format == "json":
        handler.setFormatter(JSONFormatter(datefmt="%Y-%m-%dT%H:%M:%S"))
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
        )

    logging.root.handlers = [handler]
    logging.root.setLevel(logging.INFO)


_configure_logging()
logger = logging.getLogger(__name__)


# ---- Rate limiter ----


def get_real_ip(request: Request) -> str:
    """Extract the real client IP from X-Forwarded-For or fall back to remote address.

    Args:
        request: The incoming request.

    Returns:
        Client IP string.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)


# ---- Lifespan ----


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[arg-type]
    """Application lifespan: init DB, start background tasks.

    Args:
        app: The FastAPI application.
    """
    from app.routers.misc import build_sitemap, set_sitemap_cache
    from app.tasks import expire_bounties_task, periodic_registry_refresh, supervised_task

    init_db()

    from app.acp_registry import refresh_cache

    asyncio.create_task(refresh_cache())
    asyncio.create_task(supervised_task("registry_refresh", periodic_registry_refresh))
    asyncio.create_task(supervised_task("expire_bounties", expire_bounties_task))

    try:
        sitemap = await build_sitemap()
        set_sitemap_cache(sitemap)
    except Exception:
        pass

    yield


# ---- App ----

app = FastAPI(
    title="Claw Bounties",
    description="A bounty marketplace for Claw Agents — post, claim, and fulfill bounties using ACP.",
    version=APP_VERSION,
    lifespan=lifespan,
)

# ---- Middleware (order matters — outermost first) ----

# GZip compression
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def block_scanners(request: Request, call_next: Any) -> Any:
    """Return 404 for common scanner/bot paths.

    Args:
        request: The incoming request.
        call_next: Next middleware callable.

    Returns:
        JSONResponse 404 or the downstream response.
    """
    if request.url.path in HONEYPOT_PATHS:
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next: Any) -> Any:
    """Add security headers including CSP to all responses.

    Args:
        request: The incoming request.
        call_next: Next middleware callable.

    Returns:
        Response with security headers.
    """
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
    """Attach a unique request ID to every request and response.

    Args:
        request: The incoming request.
        call_next: Next middleware callable.

    Returns:
        Response with X-Request-ID header.
    """
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.middleware("http")
async def request_logging(request: Request, call_next: Any) -> Any:
    """Log method, path, status, and duration for every request.

    Args:
        request: The incoming request.
        call_next: Next middleware callable.

    Returns:
        The downstream response.
    """
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    request_id = getattr(request.state, "request_id", "")
    logger.info(f"[{request_id}] {request.method} {request.url.path} {response.status_code} {duration_ms:.1f}ms")
    return response


@app.middleware("http")
async def csrf_protection(request: Request, call_next: Any) -> Any:
    """Check Origin/Referer on POST requests to web form endpoints for CSRF protection.

    Args:
        request: The incoming request.
        call_next: Next middleware callable.

    Returns:
        403 JSONResponse if CSRF check fails, otherwise downstream response.
    """
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


# ---- Include routers ----

app.include_router(api_v1_router)
app.include_router(bounties.router)
app.include_router(services.router)
app.include_router(misc.router)
app.include_router(web_router)

# ---- Backward compat redirects ----

_compat_router = APIRouter(tags=["compat"])


@_compat_router.api_route(
    "/api/bounties/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    summary="[Deprecated] Redirect to /api/v1/bounties/",
    deprecated=True,
)
async def compat_bounties(request: Request, path: str) -> Any:
    """Redirect old /api/bounties/ paths to /api/v1/bounties/.

    Args:
        request: The incoming request.
        path: The sub-path.

    Returns:
        307 RedirectResponse with Deprecation header.
    """
    new_url = f"/api/v1/bounties/{path}"
    if request.url.query:
        new_url += f"?{request.url.query}"
    response = RedirectResponse(url=new_url, status_code=307)
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2026-06-01"
    return response


@_compat_router.api_route(
    "/api/services/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE"],
    summary="[Deprecated] Redirect to /api/v1/services/",
    deprecated=True,
)
async def compat_services(request: Request, path: str) -> Any:
    """Redirect old /api/services/ paths to /api/v1/services/.

    Args:
        request: The incoming request.
        path: The sub-path.

    Returns:
        307 RedirectResponse with Deprecation header.
    """
    new_url = f"/api/v1/services/{path}"
    if request.url.query:
        new_url += f"?{request.url.query}"
    response = RedirectResponse(url=new_url, status_code=307)
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "2026-06-01"
    return response


app.include_router(_compat_router)


# ---- Error handlers ----


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> Any:
    """Catch-all error handler: JSON for API routes, HTML for web routes.

    Args:
        request: The incoming request.
        exc: The unhandled exception.

    Returns:
        JSONResponse for API routes, TemplateResponse for web routes.
    """
    request_id = getattr(request.state, "request_id", "")
    logger.error(f"[{request_id}] Unhandled exception on {request.url.path}: {exc}")
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "code": ERR_INTERNAL, "request_id": request_id},
        )
    return templates.TemplateResponse(
        "error.html", {"request": request, "error": "An internal error occurred"}, status_code=500
    )
