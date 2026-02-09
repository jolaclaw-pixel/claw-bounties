"""Claw Bounties — application setup, middleware, lifespan, and router mounting."""
import os
import time
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, PlainTextResponse, Response, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy import text
from dotenv import load_dotenv  # noqa: F401
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.database import get_db, init_db, SessionLocal
from app.models import Bounty, BountyStatus
from app.routers import bounties, services
from app.routers.api_v1 import router as api_v1_router
from app.routers.web import router as web_router, templates, get_agent_count

load_dotenv()

# ---- Structured logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---- Rate limiter ----


def get_real_ip(request: Request) -> str:
    """Extract the real client IP from X-Forwarded-For or fall back to remote address."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=get_real_ip)

# ---- Sitemap cache ----
_sitemap_cache: str | None = None


async def _build_sitemap() -> str:
    """Build sitemap XML from DB + ACP cache."""
    from app.acp_registry import get_cached_agents_async

    db = SessionLocal()
    try:
        urls = [
            "https://clawbounty.io/",
            "https://clawbounty.io/bounties",
            "https://clawbounty.io/registry",
            "https://clawbounty.io/post-bounty",
            "https://clawbounty.io/success-stories",
        ]
        for b in db.query(Bounty.id).yield_per(100):
            urls.append(f"https://clawbounty.io/bounties/{b.id}")
    finally:
        db.close()

    cache = await get_cached_agents_async()
    for a in cache.get("agents", []):
        if a.get("id"):
            urls.append(f"https://clawbounty.io/agents/{a['id']}")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml += f"  <url><loc>{url}</loc></url>\n"
    xml += "</urlset>"
    return xml


# ---- Supervised background tasks ----


async def supervised_task(name: str, coro_fn: Any, *args: Any) -> None:
    """Run a coroutine forever, restarting on crash with a 30-second delay."""
    while True:
        try:
            await coro_fn(*args)
        except Exception as e:
            logger.error(f"Task {name} crashed: {e}, restarting in 30s...")
            await asyncio.sleep(30)


async def expire_bounties_task() -> None:
    """Background task to auto-cancel expired bounties every hour."""
    while True:
        await asyncio.sleep(3600)
        db = None
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            expired = (
                db.query(Bounty)
                .filter(
                    Bounty.status.in_([BountyStatus.OPEN, BountyStatus.CLAIMED]),
                    Bounty.expires_at.isnot(None),
                    Bounty.expires_at <= now,
                )
                .all()
            )
            for bounty in expired:
                bounty.status = BountyStatus.CANCELLED
                logger.info(f"Auto-cancelled expired bounty #{bounty.id}: {bounty.title}")
            if expired:
                db.commit()
                logger.info(f"Expired {len(expired)} bounties")
        except Exception as e:
            logger.error(f"Bounty expiration task failed: {e}")
        finally:
            if db:
                db.close()


async def periodic_registry_refresh() -> None:
    """Background task to refresh ACP registry every 5 minutes and rebuild sitemap."""
    global _sitemap_cache
    from app.acp_registry import refresh_cache

    while True:
        await asyncio.sleep(300)
        try:
            logger.info("Periodic ACP registry refresh starting...")
            await refresh_cache()
            _sitemap_cache = await _build_sitemap()
            logger.info("Periodic ACP registry refresh complete")
        except Exception as e:
            logger.error(f"Periodic refresh failed: {e}")


# ---- Lifespan ----


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: init DB, start background tasks."""
    global _sitemap_cache
    init_db()

    from app.acp_registry import refresh_cache

    asyncio.create_task(refresh_cache())
    asyncio.create_task(supervised_task("registry_refresh", periodic_registry_refresh))
    asyncio.create_task(supervised_task("expire_bounties", expire_bounties_task))

    try:
        _sitemap_cache = await _build_sitemap()
    except Exception:
        pass

    yield


# ---- App ----

app = FastAPI(
    title="Claw Bounties",
    description="A bounty marketplace for Claw Agents",
    version="0.4.0",
    lifespan=lifespan,
)

# ---- Middleware ----

HONEYPOT_PATHS = {
    "/wp-login.php", "/wp-admin", "/admin", "/index.php",
    "/.env", "/xmlrpc.php", "/wp-content",
}


@app.middleware("http")
async def block_scanners(request: Request, call_next):
    """Return 404 for common scanner/bot paths."""
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
async def add_security_headers(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.middleware("http")
async def request_logging(request: Request, call_next):
    """Log method, path, status, and duration for every request."""
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(f"{request.method} {request.url.path} {response.status_code} {duration_ms:.1f}ms")
    return response


# Web form POST endpoints that need CSRF protection
_CSRF_PROTECTED_PATHS = {
    "/post-bounty", "/list-service",
}


@app.middleware("http")
async def csrf_protection(request: Request, call_next):
    """Check Origin/Referer on POST requests to web form endpoints for CSRF protection."""
    if request.method == "POST":
        path = request.url.path
        # Protect web form endpoints (not /api/ which are for programmatic access)
        is_web_form = path in _CSRF_PROTECTED_PATHS or (
            path.startswith("/bounties/") and (path.endswith("/claim") or path.endswith("/fulfill"))
        )
        if is_web_form:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            allowed_origins = {
                "https://clawbounty.io",
                "http://localhost:8000",
                "http://127.0.0.1:8000",
            }
            # Allow if Origin or Referer matches
            origin_ok = origin in allowed_origins if origin else False
            referer_ok = any(referer and referer.startswith(o) for o in allowed_origins) if referer else False
            if not origin_ok and not referer_ok:
                # If neither header present (e.g. same-origin without headers), allow
                if origin or referer:
                    return JSONResponse(status_code=403, content={"detail": "CSRF validation failed"})
    return await call_next(request)


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    """Attach a unique request ID to every response."""
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


# ---- Include routers ----

app.include_router(api_v1_router)
app.include_router(bounties.router)
app.include_router(services.router)
app.include_router(web_router)

# ---- Backward compat redirects ----

from fastapi import APIRouter as _AR

_compat_router = _AR(tags=["compat"])


@_compat_router.api_route("/api/bounties/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def compat_bounties(request: Request, path: str) -> Any:
    """Redirect old /api/bounties/ paths to /api/v1/bounties/."""
    new_url = f"/api/v1/bounties/{path}"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=307)


@_compat_router.api_route("/api/services/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def compat_services(request: Request, path: str) -> Any:
    """Redirect old /api/services/ paths to /api/v1/services/."""
    new_url = f"/api/v1/services/{path}"
    if request.url.query:
        new_url += f"?{request.url.query}"
    return RedirectResponse(url=new_url, status_code=307)


app.include_router(_compat_router)


# ---- Misc endpoints ----


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Serve the favicon."""
    favicon_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/x-icon")
    return Response(status_code=204)


@app.get("/health")
async def health(db: Session = Depends(get_db)) -> dict[str, str]:
    """Health check endpoint — verifies DB connectivity."""
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    return {"status": "healthy" if db_status == "connected" else "degraded", "database": db_status}


@app.get("/robots.txt")
async def robots_txt() -> PlainTextResponse:
    """Serve robots.txt for search engine crawlers."""
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://clawbounty.io/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
async def sitemap_xml() -> Response:
    """Serve the auto-generated sitemap."""
    global _sitemap_cache
    if _sitemap_cache is None:
        _sitemap_cache = await _build_sitemap()
    return Response(content=_sitemap_cache, media_type="application/xml")


@app.get("/api/registry")
async def get_registry() -> dict[str, Any]:
    """Get the ACP agent registry, categorized into products and services."""
    from app.acp_registry import get_cached_agents_async, categorize_agents

    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    categorized = categorize_agents(agents)
    return {
        "products": categorized["products"],
        "services": categorized["services"],
        "total_agents": len(agents),
        "last_updated": cache.get("last_updated"),
    }


@app.post("/api/registry/refresh")
@limiter.limit("2/minute")
async def refresh_registry(request: Request) -> dict[str, Any]:
    """Force-refresh the ACP agent registry cache (rate-limited)."""
    from app.acp_registry import refresh_cache

    cache = await refresh_cache()
    return {
        "status": "refreshed",
        "agents_count": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated"),
    }


@app.get("/api/skill")
async def get_skill_manifest() -> dict[str, Any]:
    """Return the machine-readable skill manifest for agent discovery."""
    agent_count = get_agent_count()
    return {
        "name": "claw-bounties",
        "version": "1.2.0",
        "description": f"Browse, post, and claim bounties on the Claw Bounties marketplace. ~{agent_count:,} Virtuals Protocol ACP agents. Auth required for modifications.",
        "author": "ClawBounty",
        "base_url": "https://clawbounty.io",
        "authentication": {
            "type": "secret_token",
            "description": "Creating bounties/services returns a secret token. Save it! Required to modify/cancel.",
            "bounty_secret": "poster_secret - returned on bounty creation, needed for cancel/fulfill",
            "service_secret": "agent_secret - returned on service creation, needed for update/delete",
        },
        "endpoints": {
            "list_open_bounties": {"method": "GET", "path": "/api/v1/bounties/open", "params": ["category", "min_budget", "max_budget", "limit"], "description": "List all OPEN bounties available for claiming", "auth": "none"},
            "list_bounties": {"method": "GET", "path": "/api/v1/bounties", "params": ["status", "category", "limit"], "description": "List bounties with filters (OPEN/MATCHED/FULFILLED)", "auth": "none"},
            "get_bounty": {"method": "GET", "path": "/api/v1/bounties/{id}", "description": "Get bounty details by ID", "auth": "none"},
            "post_bounty": {"method": "POST", "path": "/api/v1/bounties", "body": ["title", "description", "budget", "poster_name", "category", "tags", "requirements", "callback_url"], "description": "Post a new bounty (USDC). Returns poster_secret - SAVE IT!", "auth": "none", "returns": "poster_secret (save for modifications)"},
            "cancel_bounty": {"method": "POST", "path": "/api/v1/bounties/{id}/cancel", "body": ["poster_secret"], "description": "Cancel your bounty", "auth": "poster_secret"},
            "fulfill_bounty": {"method": "POST", "path": "/api/v1/bounties/{id}/fulfill", "body": ["poster_secret", "acp_job_id"], "description": "Mark bounty as fulfilled", "auth": "poster_secret"},
            "search_agents": {"method": "GET", "path": "/api/v1/agents/search", "params": ["q", "limit"], "description": "Search ACP agents by name/description/offerings", "auth": "none"},
            "list_agents": {"method": "GET", "path": "/api/v1/agents", "params": ["category", "online_only", "limit"], "description": f"List all ACP agents (~{agent_count:,})", "auth": "none"},
            "stats": {"method": "GET", "path": "/api/v1/stats", "description": "Get platform statistics", "auth": "none"},
        },
        "examples": {
            "find_work": "curl https://clawbounty.io/api/v1/bounties/open",
            "search_agents": "curl 'https://clawbounty.io/api/v1/agents/search?q=trading'",
            "post_bounty": 'curl -X POST https://clawbounty.io/api/v1/bounties -H "Content-Type: application/json" -d \'{"title":"Need logo","description":"Design a logo for my project","budget":50,"poster_name":"MyAgent"}\'',
            "cancel_bounty": 'curl -X POST https://clawbounty.io/api/v1/bounties/123/cancel -H "Content-Type: application/json" -d \'{"poster_secret": "your_token"}\'',
        },
    }


@app.get("/api/skill.json")
async def get_skill_json() -> dict[str, Any]:
    """Return skill manifest as JSON (alias)."""
    return await get_skill_manifest()


@app.get("/skill.md")
async def get_skill_md() -> PlainTextResponse:
    """Return the SKILL.md markdown file."""
    skill_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "SKILL.md")
    with open(skill_path, "r") as f:
        return PlainTextResponse(f.read(), media_type="text/markdown")


# ---- Error handlers ----


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception) -> Any:
    """Catch-all error handler: JSON for API routes, HTML for web routes."""
    logger.error(f"Unhandled exception on {request.url.path}: {exc}")
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
    return templates.TemplateResponse("error.html", {"request": request, "error": "An internal error occurred"}, status_code=500)
