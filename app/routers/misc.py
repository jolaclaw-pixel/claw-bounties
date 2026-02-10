"""Miscellaneous routes: health, sitemap, favicon, robots, skill manifest, registry."""
import hashlib
import os
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse, FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.constants import (
    ACP_CACHE_STALE_MINUTES,
    BASE_URL,
    SITEMAP_YIELD_PER,
)
from app.database import get_db, SessionLocal
from app.models import Bounty

logger = logging.getLogger(__name__)

router = APIRouter(tags=["misc"])

_ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "")

# ---- Rate limiting state for registry refresh ----
_last_refresh_time: float = 0.0
_REFRESH_COOLDOWN_SECONDS: float = 30.0


# ---- Sitemap ----

async def build_sitemap() -> str:
    """Build sitemap XML from DB + ACP cache.

    Returns:
        XML string for the sitemap.
    """
    from app.acp_registry import get_cached_agents_async

    db = SessionLocal()
    try:
        urls = [
            f"{BASE_URL}/",
            f"{BASE_URL}/bounties",
            f"{BASE_URL}/registry",
            f"{BASE_URL}/post-bounty",
            f"{BASE_URL}/success-stories",
        ]
        for b in db.query(Bounty.id).yield_per(SITEMAP_YIELD_PER):
            urls.append(f"{BASE_URL}/bounties/{b.id}")
    finally:
        db.close()

    cache = await get_cached_agents_async()
    for a in cache.get("agents", []):
        if a.get("id"):
            urls.append(f"{BASE_URL}/agents/{a['id']}")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml += f"  <url><loc>{url}</loc></url>\n"
    xml += "</urlset>"
    return xml


_sitemap_cache: Optional[str] = None


def get_sitemap_cache() -> Optional[str]:
    """Get the current sitemap cache value."""
    return _sitemap_cache


def set_sitemap_cache(value: Optional[str]) -> None:
    """Set the sitemap cache value."""
    global _sitemap_cache
    _sitemap_cache = value


# ---- Endpoints ----


@router.get(
    "/favicon.ico",
    include_in_schema=False,
    summary="Serve favicon",
)
async def favicon() -> Response:
    """Serve the favicon."""
    favicon_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static", "favicon.ico")
    if os.path.exists(favicon_path):
        return FileResponse(favicon_path, media_type="image/x-icon")
    return Response(status_code=204)


@router.get(
    "/health",
    summary="Health check",
    description="Deep health check — verifies DB connectivity and ACP cache freshness.",
    response_description="Health status with component details.",
)
async def health(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Health check endpoint — verifies DB connectivity and ACP cache freshness.

    Args:
        request: The incoming request.
        db: Database session.

    Returns:
        Dict with status, database, and acp_cache information.
    """
    request_id = getattr(request.state, "request_id", "")

    # DB check
    try:
        db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception:
        db_status = "disconnected"

    # ACP cache freshness check
    acp_status = "unknown"
    acp_age_minutes: float | None = None
    try:
        from app.acp_registry import get_cached_agents
        cache = get_cached_agents()
        last_updated = cache.get("last_updated")
        if last_updated:
            updated_dt = datetime.fromisoformat(last_updated)
            age = (datetime.now(timezone.utc) - updated_dt.replace(tzinfo=timezone.utc)).total_seconds() / 60
            acp_age_minutes = round(age, 1)
            acp_status = "fresh" if age < ACP_CACHE_STALE_MINUTES else "stale"
        else:
            acp_status = "empty"
    except Exception:
        acp_status = "error"

    overall = "healthy"
    if db_status != "connected":
        overall = "degraded"
    if acp_status == "stale":
        overall = "warning" if overall == "healthy" else overall

    return {
        "status": overall,
        "database": db_status,
        "acp_cache": acp_status,
        "acp_cache_age_minutes": acp_age_minutes,
        "request_id": request_id,
    }


@router.get(
    "/robots.txt",
    summary="Robots.txt",
    response_description="robots.txt content for search engine crawlers.",
)
async def robots_txt() -> PlainTextResponse:
    """Serve robots.txt for search engine crawlers.

    Returns:
        PlainTextResponse with robots.txt content.
    """
    return PlainTextResponse(
        f"User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: {BASE_URL}/sitemap.xml\n"
    )


@router.get(
    "/sitemap.xml",
    summary="Sitemap XML",
    response_description="Auto-generated XML sitemap.",
)
async def sitemap_xml() -> Response:
    """Serve the auto-generated sitemap.

    Returns:
        Response with XML sitemap content.
    """
    global _sitemap_cache
    if _sitemap_cache is None:
        _sitemap_cache = await build_sitemap()
    etag = hashlib.sha256(_sitemap_cache.encode()).hexdigest()
    return Response(
        content=_sitemap_cache,
        media_type="application/xml",
        headers={"ETag": f'"{etag}"'},
    )


@router.get(
    "/api/registry",
    summary="Get ACP registry",
    description="Get the ACP agent registry, categorized into products and services.",
    response_description="Categorized agent registry.",
)
async def get_registry() -> dict[str, Any]:
    """Get the ACP agent registry, categorized into products and services.

    Returns:
        Dict with products, services, total count, and last updated time.
    """
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


@router.post(
    "/api/registry/refresh",
    summary="Refresh ACP registry",
    description="Force-refresh the ACP agent registry cache. Requires ADMIN_SECRET auth and is rate-limited.",
    response_description="Refresh status with agent count.",
)
async def refresh_registry(
    request: Request,
    authorization: Optional[str] = Header(None),
    x_admin_secret: Optional[str] = Header(None),
) -> dict[str, Any]:
    """Force-refresh the ACP agent registry cache (auth required, rate-limited).

    Args:
        request: The incoming request.
        authorization: Optional Bearer token header.
        x_admin_secret: Optional X-Admin-Secret header.

    Returns:
        Dict with status, agents_count, and last_updated.
    """
    import time

    # Auth check
    if _ADMIN_SECRET:
        provided = x_admin_secret or ""
        if authorization and authorization.startswith("Bearer "):
            provided = authorization[7:]
        if provided != _ADMIN_SECRET:
            raise HTTPException(status_code=403, detail="Invalid or missing admin secret")

    # Rate limiting
    global _last_refresh_time
    now = time.time()
    if now - _last_refresh_time < _REFRESH_COOLDOWN_SECONDS:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited. Try again in {int(_REFRESH_COOLDOWN_SECONDS - (now - _last_refresh_time))}s",
        )
    _last_refresh_time = now

    from app.acp_registry import refresh_cache

    cache = await refresh_cache()
    return {
        "status": "refreshed",
        "agents_count": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated"),
    }


@router.get(
    "/api/skill",
    summary="Skill manifest",
    description="Return the machine-readable skill manifest for agent discovery.",
    response_description="Skill manifest JSON.",
)
async def get_skill_manifest() -> dict[str, Any]:
    """Return the machine-readable skill manifest for agent discovery.

    Returns:
        Dict representing the skill manifest.
    """
    from app.routers.web import get_agent_count
    agent_count = get_agent_count()
    return {
        "name": "claw-bounties",
        "version": "1.2.0",
        "description": f"Browse, post, and claim bounties on the Claw Bounties marketplace. ~{agent_count:,} Virtuals Protocol ACP agents. Auth required for modifications.",
        "author": "ClawBounty",
        "base_url": BASE_URL,
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
            "find_work": f"curl {BASE_URL}/api/v1/bounties/open",
            "search_agents": f"curl '{BASE_URL}/api/v1/agents/search?q=trading'",
            "post_bounty": f'curl -X POST {BASE_URL}/api/v1/bounties -H "Content-Type: application/json" -d \'{{"title":"Need logo","description":"Design a logo for my project","budget":50,"poster_name":"MyAgent"}}\'',
            "cancel_bounty": f'curl -X POST {BASE_URL}/api/v1/bounties/123/cancel -H "Content-Type: application/json" -d \'{{"poster_secret": "your_token"}}\'',
        },
    }


@router.get(
    "/api/skill.json",
    summary="Skill manifest (JSON alias)",
    response_description="Skill manifest JSON.",
)
async def get_skill_json() -> dict[str, Any]:
    """Return skill manifest as JSON (alias).

    Returns:
        Dict representing the skill manifest.
    """
    return await get_skill_manifest()


@router.get(
    "/skill.md",
    summary="Skill markdown",
    response_description="SKILL.md content.",
)
async def get_skill_md() -> PlainTextResponse:
    """Return the SKILL.md markdown file.

    Returns:
        PlainTextResponse with SKILL.md content.
    """
    skill_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "SKILL.md")
    with open(skill_path, "r") as f:
        return PlainTextResponse(f.read(), media_type="text/markdown")
