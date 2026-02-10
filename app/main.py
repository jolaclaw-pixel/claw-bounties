"""Claw Bounties — application setup, middleware, lifespan, and router mounting."""
import asyncio
import json
import logging
import os
import sys
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
    APP_VERSION,
    ERR_INTERNAL,
)
from app.database import init_db
from app.middleware import register_middleware
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
    """Extract the real client IP from X-Forwarded-For or fall back to remote address."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)


# ---- Lifespan ----


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[arg-type]
    """Application lifespan: init DB, start background tasks."""
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

# Register all HTTP middleware from middleware module
register_middleware(app)

_cors_origins = os.getenv("CORS_ORIGINS", "https://clawbounty.io,http://localhost:8000,http://127.0.0.1:8000")
_allowed_cors_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.mount("/static", StaticFiles(directory="static"), name="static")


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
    """Redirect old /api/bounties/ paths to /api/v1/bounties/."""
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
    """Redirect old /api/services/ paths to /api/v1/services/."""
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
    """Catch-all error handler: JSON for API routes, HTML for web routes."""
    request_id = getattr(request.state, "request_id", "")
    logger.error("[%s] Unhandled exception on %s: %s", request_id, request.url.path, exc)
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "code": ERR_INTERNAL, "request_id": request_id},
        )
    return templates.TemplateResponse(
        request=request, name="error.html", context={"error": "An internal error occurred"}, status_code=500
    )
