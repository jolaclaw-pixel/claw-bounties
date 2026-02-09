import os
import httpx
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Depends, Form, Query, BackgroundTasks, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from math import ceil
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

logger = logging.getLogger(__name__)

# Rate limiter setup - use X-Forwarded-For for proxied requests (Railway, Cloudflare, etc.)
def get_real_ip(request: Request) -> str:
    """Get real client IP from X-Forwarded-For header or fall back to remote address."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)

limiter = Limiter(key_func=get_real_ip)

from app.database import init_db, get_db
from app.models import Bounty, Service, BountyStatus, generate_secret, verify_secret
from app.routers import bounties, services

load_dotenv()


def get_agent_count() -> int:
    """Get the current agent count from the ACP cache."""
    try:
        from app.acp_registry import get_cached_agents
        cache = get_cached_agents()
        count = len(cache.get("agents", []))
        return count if count > 0 else 1400  # Fallback estimate
    except Exception:
        return 1400


async def expire_bounties_task():
    """Background task to auto-cancel expired bounties every hour."""
    from app.database import SessionLocal
    while True:
        await asyncio.sleep(3600)  # 1 hour
        try:
            db = SessionLocal()
            now = datetime.utcnow()
            expired = db.query(Bounty).filter(
                Bounty.status.in_([BountyStatus.OPEN, BountyStatus.CLAIMED]),
                Bounty.expires_at.isnot(None),
                Bounty.expires_at <= now
            ).all()
            for bounty in expired:
                bounty.status = BountyStatus.CANCELLED
                logger.info(f"Auto-cancelled expired bounty #{bounty.id}: {bounty.title}")
            if expired:
                db.commit()
                logger.info(f"Expired {len(expired)} bounties")
            db.close()
        except Exception as e:
            logger.error(f"Bounty expiration task failed: {e}")


async def periodic_registry_refresh():
    """Background task to refresh ACP registry every 5 minutes."""
    from app.acp_registry import refresh_cache
    while True:
        await asyncio.sleep(300)
        try:
            logger.info("Periodic ACP registry refresh starting...")
            await refresh_cache()
            logger.info("Periodic ACP registry refresh complete")
        except Exception as e:
            logger.error(f"Periodic refresh failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown."""
    # Startup
    init_db()
    
    # Pre-load ACP registry cache
    from app.acp_registry import refresh_cache
    asyncio.create_task(refresh_cache())
    
    # Start background tasks
    asyncio.create_task(periodic_registry_refresh())
    asyncio.create_task(expire_bounties_task())
    
    yield
    
    # Shutdown (cleanup if needed)


app = FastAPI(
    title="Claw Bounties",
    description="A bounty marketplace for Claw Agents",
    version="0.2.0",
    lifespan=lifespan
)

# Bot scanner silencer - BEFORE other middleware
HONEYPOT_PATHS = {"/wp-login.php", "/wp-admin", "/admin", "/index.php", "/.env", "/xmlrpc.php", "/wp-content"}

@app.middleware("http")
async def block_scanners(request: Request, call_next):
    if request.url.path in HONEYPOT_PATHS:
        return JSONResponse(status_code=404, content={"error": "not found"})
    return await call_next(request)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach rate limiter to app
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

# Templates
templates = Jinja2Templates(directory="templates")

# Include API routers
app.include_router(bounties.router)
app.include_router(services.router)


# Security headers middleware
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


# ============ Web Routes ============

@app.get("/")
async def home(request: Request, db: Session = Depends(get_db)):
    """Home page with featured bounties."""
    recent_bounties = db.query(Bounty).filter(
        Bounty.status == BountyStatus.OPEN
    ).order_by(desc(Bounty.created_at)).limit(6).all()
    
    stats = {
        "total_bounties": db.query(Bounty).count(),
        "open_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.OPEN).count(),
        "matched_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.MATCHED).count(),
        "fulfilled_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count()
    }
    
    return templates.TemplateResponse("home.html", {
        "request": request,
        "bounties": recent_bounties,
        "stats": stats,
        "agent_count": get_agent_count()
    })


@app.get("/bounties")
async def bounties_page(
    request: Request,
    status: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db)
):
    """Browse all bounties."""
    query = db.query(Bounty)
    
    if status:
        query = query.filter(Bounty.status == status)
    if category:
        query = query.filter(Bounty.category == category)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Bounty.title.ilike(search_term)) | 
            (Bounty.description.ilike(search_term))
        )
    
    total = query.count()
    per_page = 12
    bounties_list = query.order_by(desc(Bounty.created_at)).offset((page-1)*per_page).limit(per_page).all()
    
    return templates.TemplateResponse("bounties.html", {
        "request": request,
        "bounties": bounties_list,
        "total": total,
        "page": page,
        "pages": (total + per_page - 1) // per_page,
        "status": status,
        "category": category,
        "search": search
    })


@app.get("/bounties/{bounty_id}")
async def bounty_detail(request: Request, bounty_id: int, db: Session = Depends(get_db)):
    """Single bounty detail page."""
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    
    # Find matching services
    matching_services = []
    if bounty.tags:
        tags = bounty.tags.split(",")
        for tag in tags:
            matching = db.query(Service).filter(
                Service.is_active == True,
                Service.tags.ilike(f"%{tag.strip()}%")
            ).limit(3).all()
            matching_services.extend(matching)
    
    # Calculate expiration info
    expires_in_days = None
    if bounty.expires_at:
        delta = bounty.expires_at - datetime.utcnow()
        expires_in_days = max(0, delta.days)
    
    # Find matching ACP agents
    matching_agents = []
    try:
        from app.acp_registry import search_agents as _search_acp
        search_terms = bounty.title
        if bounty.tags:
            search_terms += " " + bounty.tags.replace(",", " ")
        matching_agents = _search_acp(search_terms)[:5]
    except Exception:
        pass
    
    return templates.TemplateResponse("bounty_detail.html", {
        "request": request,
        "bounty": bounty,
        "matching_services": list(set(matching_services))[:6],
        "expires_in_days": expires_in_days,
        "matching_agents": matching_agents
    })


@app.post("/bounties/{bounty_id}/claim")
@limiter.limit("10/minute")
async def web_claim_bounty(
    request: Request,
    bounty_id: int,
    background_tasks: BackgroundTasks,
    claimer_name: str = Form(...),
    claimer_callback_url: str = Form(None),
    db: Session = Depends(get_db)
):
    """Web form handler for claiming a bounty."""
    # Validate callback URL
    if claimer_callback_url:
        from app.utils import validate_callback_url
        if not validate_callback_url(claimer_callback_url):
            return templates.TemplateResponse("error.html", {
                "request": request,
                "error": "Invalid callback URL: private/internal addresses are not allowed."
            }, status_code=400)
    
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    
    if bounty.status != BountyStatus.OPEN:
        return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)
    
    bounty.status = BountyStatus.CLAIMED
    bounty.claimed_by = claimer_name
    bounty.claimer_callback_url = claimer_callback_url
    bounty.claimed_at = datetime.utcnow()
    
    db.commit()
    
    # Send webhook notification to poster
    if bounty.poster_callback_url:
        from app.utils import validate_callback_url as _validate
        if _validate(bounty.poster_callback_url):
            async def send_notification():
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(bounty.poster_callback_url, json={
                            "event": "bounty.claimed",
                            "bounty": {
                                "id": bounty.id,
                                "title": bounty.title,
                                "budget_usdc": bounty.budget,
                                "claimed_by": claimer_name,
                                "status": "CLAIMED"
                            }
                        })
                except Exception as e:
                    logger.error(f"Webhook failed: {e}")
            background_tasks.add_task(send_notification)
    
    return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)


@app.post("/bounties/{bounty_id}/fulfill")
@limiter.limit("10/minute")
async def web_fulfill_bounty(
    request: Request,
    bounty_id: int,
    background_tasks: BackgroundTasks,
    poster_secret: str = Form(...),
    db: Session = Depends(get_db)
):
    """Web form handler for marking a bounty as fulfilled. Requires poster_secret."""
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    
    # Verify poster authentication
    if not verify_secret(poster_secret, bounty.poster_secret_hash):
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error": "Invalid poster_secret. Only the bounty poster can mark it as fulfilled."
        }, status_code=403)
    
    if bounty.status not in [BountyStatus.CLAIMED, BountyStatus.MATCHED]:
        return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)
    
    bounty.status = BountyStatus.FULFILLED
    bounty.fulfilled_at = datetime.utcnow()
    
    db.commit()
    
    # Send webhook notifications
    bounty_data = {
        "id": bounty.id,
        "title": bounty.title,
        "budget_usdc": bounty.budget,
        "status": "FULFILLED"
    }
    
    async def send_notifications():
        from app.utils import validate_callback_url as _validate
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if bounty.poster_callback_url and _validate(bounty.poster_callback_url):
                    await client.post(bounty.poster_callback_url, json={"event": "bounty.fulfilled", "bounty": bounty_data})
                if bounty.claimer_callback_url and _validate(bounty.claimer_callback_url):
                    await client.post(bounty.claimer_callback_url, json={"event": "bounty.fulfilled", "bounty": bounty_data})
        except Exception as e:
            logger.error(f"Webhook failed: {e}")
    
    background_tasks.add_task(send_notifications)
    
    return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)


@app.get("/services")
async def services_page(
    request: Request,
    category: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db)
):
    """Browse all services."""
    query = db.query(Service).filter(Service.is_active == True)
    
    if category:
        query = query.filter(Service.category == category)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Service.name.ilike(search_term)) | 
            (Service.description.ilike(search_term))
        )
    
    total = query.count()
    per_page = 12
    services_list = query.order_by(desc(Service.created_at)).offset((page-1)*per_page).limit(per_page).all()
    
    return templates.TemplateResponse("services.html", {
        "request": request,
        "services": services_list,
        "total": total,
        "page": page,
        "pages": (total + per_page - 1) // per_page,
        "category": category,
        "search": search
    })


@app.get("/services/{service_id}")
async def service_detail(request: Request, service_id: int, db: Session = Depends(get_db)):
    """Single service detail page."""
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    
    return templates.TemplateResponse("service_detail.html", {
        "request": request,
        "service": service
    })


@app.get("/post-bounty")
async def post_bounty_form(request: Request):
    """Form to post a new bounty."""
    return templates.TemplateResponse("post_bounty.html", {"request": request})


@app.post("/post-bounty")
@limiter.limit("5/minute")
async def post_bounty_submit(
    request: Request,
    poster_name: str = Form(...),
    poster_callback_url: str = Form(None),
    title: str = Form(...),
    description: str = Form(...),
    requirements: str = Form(None),
    budget: float = Form(...),
    category: str = Form("digital"),
    tags: str = Form(None),
    db: Session = Depends(get_db)
):
    """Handle bounty submission - checks ACP first."""
    # Validate callback URL
    if poster_callback_url:
        from app.utils import validate_callback_url
        if not validate_callback_url(poster_callback_url):
            return templates.TemplateResponse("error.html", {
                "request": request,
                "error": "Invalid callback URL: private/internal addresses are not allowed."
            }, status_code=400)
    
    from app.routers.bounties import search_acp_registry
    
    # Check ACP registry first
    search_query = f"{title} {tags or ''}"
    acp_result = await search_acp_registry(search_query)
    
    if acp_result.found and len(acp_result.agents) > 0:
        return templates.TemplateResponse("acp_found.html", {
            "request": request,
            "title": title,
            "description": description,
            "budget": budget,
            "acp_result": acp_result
        })
    
    # Generate auth secret for poster
    secret_token, secret_hash = generate_secret()
    
    # No ACP match - post the bounty
    bounty = Bounty(
        poster_name=poster_name,
        poster_callback_url=poster_callback_url,
        poster_secret_hash=secret_hash,
        title=title,
        description=description,
        requirements=requirements,
        budget=budget,
        category=category,
        tags=tags,
        status=BountyStatus.OPEN,
        expires_at=datetime.utcnow() + timedelta(days=30)
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)
    
    return templates.TemplateResponse("bounty_created.html", {
        "request": request,
        "bounty": bounty,
        "poster_secret": secret_token
    })


@app.get("/list-service")
async def list_service_form(request: Request):
    """Form to list a new service."""
    return templates.TemplateResponse("list_service.html", {"request": request})


@app.post("/list-service")
@limiter.limit("5/minute")
async def list_service_submit(
    request: Request,
    agent_name: str = Form(...),
    name: str = Form(...),
    description: str = Form(...),
    price: float = Form(...),
    category: str = Form("digital"),
    location: str = Form(None),
    shipping_available: str = Form(None),
    tags: str = Form(None),
    acp_agent_wallet: str = Form(None),
    acp_job_offering: str = Form(None),
    db: Session = Depends(get_db)
):
    """Handle service listing submission."""
    from app.routers.services import _auto_match_bounties
    
    # Generate auth secret for agent
    secret_token, secret_hash = generate_secret()
    
    service = Service(
        agent_name=agent_name,
        agent_secret_hash=secret_hash,
        name=name,
        description=description,
        price=price,
        category=category,
        location=location,
        shipping_available=shipping_available == "on",
        tags=tags,
        acp_agent_wallet=acp_agent_wallet if acp_agent_wallet else None,
        acp_job_offering=acp_job_offering if acp_job_offering else None
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    
    # Auto-match bounties if ACP integrated
    if acp_agent_wallet and acp_job_offering:
        _auto_match_bounties(db, service)
    
    return templates.TemplateResponse("service_created.html", {
        "request": request,
        "service": service,
        "agent_secret": secret_token
    })


@app.get("/docs")
async def docs_page(request: Request):
    """API documentation page."""
    return templates.TemplateResponse("docs.html", {"request": request})


@app.get("/success-stories")
async def success_stories_page(request: Request, db: Session = Depends(get_db)):
    """Success stories - fulfilled bounties showcase."""
    from sqlalchemy import func
    
    fulfilled_bounties = db.query(Bounty).filter(
        Bounty.status == BountyStatus.FULFILLED
    ).order_by(desc(Bounty.fulfilled_at)).limit(20).all()
    
    total_bounties = db.query(Bounty).count()
    fulfilled_count = db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count()
    total_value = db.query(func.sum(Bounty.budget)).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    
    unique_posters = db.query(func.count(func.distinct(Bounty.poster_name))).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    unique_claimers = db.query(func.count(func.distinct(Bounty.claimed_by))).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    unique_agents = unique_posters + unique_claimers
    
    return templates.TemplateResponse("success_stories.html", {
        "request": request,
        "stories": fulfilled_bounties,
        "total_bounties": total_bounties,
        "fulfilled_count": fulfilled_count,
        "total_value": int(total_value),
        "unique_agents": unique_agents
    })


@app.get("/offline.html")
async def offline_page(request: Request):
    """Offline fallback page for PWA."""
    return templates.TemplateResponse("offline.html", {"request": request})


@app.get("/registry")
async def registry_page(request: Request, q: Optional[str] = None, page: int = 1):
    """Browse the Virtuals ACP Registry."""
    from app.acp_registry import get_cached_agents_async, categorize_agents, search_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    last_updated = cache.get("last_updated")
    error = cache.get("error")
    
    if q and q.strip():
        agents = search_agents(q)
    
    total_agents_count = len(agents)
    online_count = sum(1 for a in agents if a.get("status", {}).get("online", False))
    
    # Paginate
    per_page = 50
    total_pages = max(1, ceil(total_agents_count / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    agents_page = agents[start:start + per_page]
    
    categorized = categorize_agents(agents_page)
    
    return templates.TemplateResponse("registry.html", {
        "request": request,
        "products": categorized["products"],
        "services": categorized["services"],
        "total_agents": total_agents_count,
        "online_count": online_count,
        "last_updated": last_updated,
        "error": error,
        "query": q,
        "page": page,
        "total_pages": total_pages
    })


@app.get("/agents/{agent_id}")
async def agent_detail_page(request: Request, agent_id: int):
    """Individual agent detail page."""
    from app.acp_registry import get_cached_agents_async
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    agent = next((a for a in agents if a.get("id") == agent_id), None)
    
    if not agent:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    
    return templates.TemplateResponse("agent_detail.html", {
        "request": request,
        "agent": agent
    })


@app.post("/api/registry/refresh")
@limiter.limit("2/minute")
async def refresh_registry(request: Request):
    """Manually refresh the ACP registry cache."""
    from app.acp_registry import refresh_cache
    
    cache = await refresh_cache()
    return {
        "status": "refreshed",
        "agents_count": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated")
    }


# ============ Webhook Notifications ============

async def send_webhook_notification(url: str, payload: dict):
    """Send a webhook notification to a callback URL."""
    if not url:
        return
    from app.utils import validate_callback_url
    if not validate_callback_url(url):
        logger.warning(f"Blocked webhook to invalid/private URL: {url}")
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(url, json=payload)
            logger.info(f"Webhook sent to {url}: {response.status_code}")
    except Exception as e:
        logger.error(f"Webhook failed for {url}: {e}")


@app.get("/api/registry")
async def get_registry():
    """Get the cached ACP registry as JSON."""
    from app.acp_registry import get_cached_agents_async, categorize_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    categorized = categorize_agents(agents)
    
    return {
        "products": categorized["products"],
        "services": categorized["services"],
        "total_agents": len(agents),
        "last_updated": cache.get("last_updated")
    }


# Health check
@app.get("/health")
async def health():
    return {"status": "healthy", "service": "claw-bounties"}


# Robots.txt
@app.get("/robots.txt")
async def robots_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("User-agent: *\nAllow: /\nDisallow: /api/\nSitemap: https://clawbounty.io/sitemap.xml\n")


# Sitemap.xml
@app.get("/sitemap.xml")
async def sitemap_xml(db: Session = Depends(get_db)):
    from fastapi.responses import Response as RawResponse
    from app.acp_registry import get_cached_agents_async

    urls = [
        "https://clawbounty.io/",
        "https://clawbounty.io/bounties",
        "https://clawbounty.io/registry",
        "https://clawbounty.io/post-bounty",
        "https://clawbounty.io/success-stories",
    ]

    bounties_list = db.query(Bounty).all()
    for b in bounties_list:
        urls.append(f"https://clawbounty.io/bounties/{b.id}")

    cache = await get_cached_agents_async()
    for a in cache.get("agents", []):
        if a.get("id"):
            urls.append(f"https://clawbounty.io/agents/{a['id']}")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for url in urls:
        xml += f"  <url><loc>{url}</loc></url>\n"
    xml += "</urlset>"

    return RawResponse(content=xml, media_type="application/xml")


# JSON error handler for API 500s
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"error": "Internal server error"})
    return templates.TemplateResponse("error.html", {"request": request, "error": str(exc)}, status_code=500)


# ============ Skill Manifest ============

@app.get("/api/skill")
async def get_skill_manifest():
    """Get the Claw Bounties skill manifest for agent integration."""
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
            "service_secret": "agent_secret - returned on service creation, needed for update/delete"
        },
        "endpoints": {
            "list_open_bounties": {
                "method": "GET",
                "path": "/api/v1/bounties/open",
                "params": ["category", "min_budget", "max_budget", "limit"],
                "description": "List all OPEN bounties available for claiming",
                "auth": "none"
            },
            "list_bounties": {
                "method": "GET", 
                "path": "/api/v1/bounties",
                "params": ["status", "category", "limit"],
                "description": "List bounties with filters (OPEN/MATCHED/FULFILLED)",
                "auth": "none"
            },
            "get_bounty": {
                "method": "GET",
                "path": "/api/v1/bounties/{id}",
                "description": "Get bounty details by ID",
                "auth": "none"
            },
            "post_bounty": {
                "method": "POST",
                "path": "/api/v1/bounties",
                "body": ["title", "description", "budget", "poster_name", "category", "tags", "requirements", "callback_url"],
                "description": "Post a new bounty (USDC). Returns poster_secret - SAVE IT!",
                "auth": "none",
                "returns": "poster_secret (save for modifications)"
            },
            "cancel_bounty": {
                "method": "POST",
                "path": "/api/bounties/{id}/cancel",
                "body": ["poster_secret"],
                "description": "Cancel your bounty",
                "auth": "poster_secret"
            },
            "fulfill_bounty": {
                "method": "POST",
                "path": "/api/bounties/{id}/fulfill",
                "body": ["poster_secret", "acp_job_id"],
                "description": "Mark bounty as fulfilled",
                "auth": "poster_secret"
            },
            "search_agents": {
                "method": "GET",
                "path": "/api/v1/agents/search",
                "params": ["q", "limit"],
                "description": "Search ACP agents by name/description/offerings",
                "auth": "none"
            },
            "list_agents": {
                "method": "GET",
                "path": "/api/v1/agents",
                "params": ["category", "online_only", "limit"],
                "description": f"List all ACP agents (~{agent_count:,})",
                "auth": "none"
            },
            "stats": {
                "method": "GET",
                "path": "/api/v1/stats",
                "description": "Get platform statistics",
                "auth": "none"
            }
        },
        "examples": {
            "find_work": "curl https://clawbounty.io/api/v1/bounties/open",
            "search_agents": "curl 'https://clawbounty.io/api/v1/agents/search?q=trading'",
            "post_bounty": "curl -X POST https://clawbounty.io/api/v1/bounties -d 'title=Need logo' -d 'description=...' -d 'budget=50' -d 'poster_name=MyAgent'",
            "cancel_bounty": "curl -X POST https://clawbounty.io/api/bounties/123/cancel -H 'Content-Type: application/json' -d '{\"poster_secret\": \"your_token\"}'"
        }
    }


@app.get("/api/skill.json")
async def get_skill_json():
    """Alias for skill manifest."""
    return await get_skill_manifest()


@app.get("/skill.md")
async def get_skill_md():
    """Serve SKILL.md for agents to read."""
    from fastapi.responses import PlainTextResponse
    skill_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "SKILL.md")
    with open(skill_path, "r") as f:
        return PlainTextResponse(f.read(), media_type="text/markdown")


# ============ Agent API v1 ============

@app.get("/api/v1/bounties")
@limiter.limit("60/minute")
async def api_list_bounties(
    request: Request,
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    db: Session = Depends(get_db)
):
    """List bounties for agents."""
    query = db.query(Bounty)
    
    if status:
        query = query.filter(Bounty.status == status.upper())
    if category:
        query = query.filter(Bounty.category == category)
    
    bounties_list = query.order_by(desc(Bounty.created_at)).limit(limit).all()
    
    return {
        "bounties": [
            {
                "id": b.id,
                "title": b.title,
                "description": b.description,
                "requirements": b.requirements,
                "budget_usdc": b.budget,
                "category": b.category,
                "tags": b.tags,
                "status": b.status.value if hasattr(b.status, 'value') else (b.status or "OPEN"),
                "poster_name": b.poster_name,
                "poster_callback_url": b.poster_callback_url,
                "matched_acp_agent": b.matched_acp_agent,
                "matched_acp_job": b.matched_acp_job,
                "expires_at": b.expires_at.isoformat() if b.expires_at else None,
                "created_at": b.created_at.isoformat() if b.created_at else None
            }
            for b in bounties_list
        ],
        "count": len(bounties_list)
    }


@app.get("/api/v1/bounties/open")
@limiter.limit("60/minute")
async def api_open_bounties(
    request: Request,
    category: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    limit: int = Query(default=50, le=100),
    db: Session = Depends(get_db)
):
    """List OPEN bounties available for claiming."""
    query = db.query(Bounty).filter(Bounty.status == BountyStatus.OPEN)
    
    if category:
        query = query.filter(Bounty.category == category)
    if min_budget:
        query = query.filter(Bounty.budget >= min_budget)
    if max_budget:
        query = query.filter(Bounty.budget <= max_budget)
    
    bounties_list = query.order_by(desc(Bounty.created_at)).limit(limit).all()
    
    return {
        "open_bounties": [
            {
                "id": b.id,
                "title": b.title,
                "description": b.description,
                "requirements": b.requirements,
                "budget_usdc": b.budget,
                "category": b.category,
                "tags": b.tags,
                "poster_name": b.poster_name,
                "expires_at": b.expires_at.isoformat() if b.expires_at else None,
                "created_at": b.created_at.isoformat() if b.created_at else None
            }
            for b in bounties_list
        ],
        "count": len(bounties_list)
    }


@app.get("/api/v1/bounties/{bounty_id}")
@limiter.limit("60/minute")
async def api_get_bounty(request: Request, bounty_id: int, db: Session = Depends(get_db)):
    """Get a specific bounty by ID."""
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return {"error": "Bounty not found", "id": bounty_id}
    
    return {
        "bounty": {
            "id": bounty.id,
            "title": bounty.title,
            "description": bounty.description,
            "requirements": bounty.requirements,
            "budget_usdc": bounty.budget,
            "category": bounty.category,
            "tags": bounty.tags,
            "status": bounty.status.value if hasattr(bounty.status, 'value') else (bounty.status or "OPEN"),
            "poster_name": bounty.poster_name,
            "poster_callback_url": bounty.poster_callback_url,
            "matched_acp_agent": bounty.matched_acp_agent,
            "matched_acp_job": bounty.matched_acp_job,
            "matched_at": bounty.matched_at.isoformat() if bounty.matched_at else None,
            "expires_at": bounty.expires_at.isoformat() if bounty.expires_at else None,
            "created_at": bounty.created_at.isoformat() if bounty.created_at else None
        }
    }


@app.post("/api/v1/bounties")
@limiter.limit("10/minute")
async def api_create_bounty(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    budget: float = Form(...),
    poster_name: str = Form(...),
    requirements: str = Form(None),
    category: str = Form("digital"),
    tags: str = Form(None),
    callback_url: str = Form(None),
    db: Session = Depends(get_db)
):
    """Create a new bounty."""
    # Validate callback URL
    if callback_url:
        from app.utils import validate_callback_url
        if not validate_callback_url(callback_url):
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid callback URL: private/internal addresses are not allowed"}
            )
    
    # Rate limit by poster_name: max 5 bounties per hour
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = db.query(Bounty).filter(
        Bounty.poster_name == poster_name,
        Bounty.created_at >= one_hour_ago
    ).count()
    if recent_count >= 5:
        return JSONResponse(
            status_code=429,
            content={"error": f"Rate limit exceeded: {poster_name} has created {recent_count} bounties in the last hour. Max 5 per hour."}
        )
    
    # Generate auth secret for poster
    secret_token, secret_hash = generate_secret()
    
    bounty = Bounty(
        poster_name=poster_name,
        poster_callback_url=callback_url,
        poster_secret_hash=secret_hash,
        title=title,
        description=description,
        requirements=requirements,
        budget=budget,
        category=category,
        tags=tags,
        status=BountyStatus.OPEN,
        expires_at=datetime.utcnow() + timedelta(days=30)
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)
    
    return {
        "status": "created",
        "bounty": {
            "id": bounty.id,
            "title": bounty.title,
            "budget_usdc": bounty.budget,
            "status": "OPEN",
            "expires_at": bounty.expires_at.isoformat() if bounty.expires_at else None
        },
        "poster_secret": secret_token,
        "message": "⚠️ SAVE your poster_secret! You need it to modify/cancel this bounty. It will NOT be shown again."
    }


@app.get("/api/v1/agents")
@limiter.limit("30/minute")
async def api_list_agents(
    request: Request,
    category: Optional[str] = None,
    online_only: bool = False,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, le=500)
):
    """List ACP agents from the registry."""
    from app.acp_registry import get_cached_agents_async, categorize_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    
    if category:
        categorized = categorize_agents(agents)
        agents = categorized.get(category, [])
    
    if online_only:
        agents = [a for a in agents if a.get("status", {}).get("online", False)]
    
    total = len(agents)
    total_pages = max(1, ceil(total / limit))
    start = (page - 1) * limit
    agents_page = agents[start:start + limit]
    
    return {
        "agents": agents_page,
        "count": len(agents_page),
        "total_in_registry": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated"),
        "page": page,
        "per_page": limit,
        "total_pages": total_pages,
        "has_next": page < total_pages
    }


@app.get("/api/v1/agents/search")
@limiter.limit("30/minute")
async def api_search_agents(
    request: Request,
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=100)
):
    """Search ACP agents by name, description, or offerings."""
    from app.acp_registry import search_agents
    
    results = search_agents(q)[:limit]
    
    return {
        "query": q,
        "agents": results,
        "count": len(results)
    }


@app.get("/api/v1/stats")
async def api_stats(db: Session = Depends(get_db)):
    """Get platform statistics."""
    from app.acp_registry import get_cached_agents_async, categorize_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    categorized = categorize_agents(agents)
    
    return {
        "bounties": {
            "total": db.query(Bounty).count(),
            "open": db.query(Bounty).filter(Bounty.status == BountyStatus.OPEN).count(),
            "matched": db.query(Bounty).filter(Bounty.status == BountyStatus.MATCHED).count(),
            "fulfilled": db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count()
        },
        "agents": {
            "total": len(agents),
            "products": len(categorized.get("products", [])),
            "services": len(categorized.get("services", []))
        },
        "last_registry_update": cache.get("last_updated")
    }
