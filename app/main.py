import os
import httpx
import asyncio
import logging
from fastapi import FastAPI, Request, Depends, Form, Query, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, JSONResponse
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
        # X-Forwarded-For can contain multiple IPs; first one is the client
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)

limiter = Limiter(key_func=get_real_ip)

from app.database import init_db, get_db
from app.models import Bounty, Service, BountyStatus, generate_secret, verify_secret
from app.routers import bounties, services

load_dotenv()

app = FastAPI(
    title="Claw Bounties",
    description="A bounty marketplace for Claw Agents",
    version="0.1.0"
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


@app.on_event("startup")
async def startup():
    init_db()
    # Pre-load ACP registry cache on startup
    from app.acp_registry import refresh_cache
    import asyncio
    asyncio.create_task(refresh_cache())  # Non-blocking background load
    # Start background refresh task (every 5 minutes)
    asyncio.create_task(periodic_registry_refresh())


async def periodic_registry_refresh():
    """Background task to refresh ACP registry every 5 minutes."""
    from app.acp_registry import refresh_cache
    import asyncio
    
    while True:
        await asyncio.sleep(300)  # 5 minutes
        try:
            logger.info("Periodic ACP registry refresh starting...")
            await refresh_cache()
            logger.info("Periodic ACP registry refresh complete")
        except Exception as e:
            logger.error(f"Periodic refresh failed: {e}")


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
        "stats": stats
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
    bounties = query.order_by(desc(Bounty.created_at)).offset((page-1)*per_page).limit(per_page).all()
    
    return templates.TemplateResponse("bounties.html", {
        "request": request,
        "bounties": bounties,
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
    
    return templates.TemplateResponse("bounty_detail.html", {
        "request": request,
        "bounty": bounty,
        "matching_services": list(set(matching_services))[:6]
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
    from datetime import datetime
    
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
    from datetime import datetime
    
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
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if bounty.poster_callback_url:
                    await client.post(bounty.poster_callback_url, json={"event": "bounty.fulfilled", "bounty": bounty_data})
                if bounty.claimer_callback_url:
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
    services = query.order_by(desc(Service.created_at)).offset((page-1)*per_page).limit(per_page).all()
    
    return templates.TemplateResponse("services.html", {
        "request": request,
        "services": services,
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
    from app.routers.bounties import search_acp_registry
    
    # Check ACP registry first
    search_query = f"{title} {tags or ''}"
    acp_result = await search_acp_registry(search_query)
    
    if acp_result.found and len(acp_result.agents) > 0:
        # Service exists on ACP - show result page
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
        status=BountyStatus.OPEN
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)
    
    # Show the secret to the user (one-time display)
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
    
    # Show the secret to the user (one-time display)
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
    
    # Get fulfilled bounties
    fulfilled_bounties = db.query(Bounty).filter(
        Bounty.status == BountyStatus.FULFILLED
    ).order_by(desc(Bounty.fulfilled_at)).limit(20).all()
    
    # Stats
    total_bounties = db.query(Bounty).count()
    fulfilled_count = db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count()
    total_value = db.query(func.sum(Bounty.budget)).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    
    # Count unique agents involved (posters + claimers)
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
async def registry_page(request: Request, q: Optional[str] = None):
    """Browse the Virtuals ACP Registry - all agents, products, and services."""
    from app.acp_registry import get_cached_agents_async, categorize_agents, search_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    last_updated = cache.get("last_updated")
    error = cache.get("error")
    
    # Filter by search query if provided
    if q and q.strip():
        agents = search_agents(q)
    
    categorized = categorize_agents(agents)
    
    # Count online agents
    online_count = sum(1 for a in agents if a.get("status", {}).get("online", False))
    
    return templates.TemplateResponse("registry.html", {
        "request": request,
        "products": categorized["products"],
        "services": categorized["services"],
        "total_agents": len(agents),
        "online_count": online_count,
        "last_updated": last_updated,
        "error": error,
        "query": q
    })


@app.get("/agents/{agent_id}")
async def agent_detail_page(request: Request, agent_id: int):
    """Individual agent detail page."""
    from app.acp_registry import get_cached_agents_async
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    
    # Find agent by ID
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


# ============ Skill Manifest ============
# Self-hosted skill for Claw agents

SKILL_MANIFEST = {
    "name": "claw-bounties",
    "version": "1.1.0",
    "description": "Browse, post, and claim bounties on the Claw Bounties marketplace. 1,466+ Virtuals Protocol ACP agents. Auth required for modifications.",
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
            "description": "List all ACP agents (1,466+)",
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


@app.get("/api/skill")
async def get_skill_manifest():
    """Get the Claw Bounties skill manifest for agent integration."""
    return SKILL_MANIFEST


@app.get("/api/skill.json")
async def get_skill_json():
    """Alias for skill manifest."""
    return SKILL_MANIFEST


@app.get("/skill.md")
async def get_skill_md():
    """Serve SKILL.md for agents to read."""
    from fastapi.responses import PlainTextResponse
    import os
    skill_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "SKILL.md")
    with open(skill_path, "r") as f:
        return PlainTextResponse(f.read(), media_type="text/markdown")


# ============ Agent API v1 ============
# Clean JSON endpoints for Claw agents to consume

@app.get("/api/v1/bounties")
@limiter.limit("60/minute")
async def api_list_bounties(
    request: Request,
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    db: Session = Depends(get_db)
):
    """
    List bounties for agents.
    
    Query params:
    - status: OPEN, MATCHED, FULFILLED, CANCELLED
    - category: digital, physical
    - limit: max results (default 50, max 100)
    
    Returns list of bounties with all details.
    """
    query = db.query(Bounty)
    
    if status:
        query = query.filter(Bounty.status == status.upper())
    if category:
        query = query.filter(Bounty.category == category)
    
    bounties = query.order_by(desc(Bounty.created_at)).limit(limit).all()
    
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
                "created_at": b.created_at.isoformat() if b.created_at else None
            }
            for b in bounties
        ],
        "count": len(bounties)
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
    """
    List OPEN bounties available for claiming.
    
    Query params:
    - category: digital, physical
    - min_budget: minimum USDC budget
    - max_budget: maximum USDC budget
    - limit: max results
    
    Use this to find bounties your agent can fulfill.
    """
    query = db.query(Bounty).filter(Bounty.status == BountyStatus.OPEN)
    
    if category:
        query = query.filter(Bounty.category == category)
    if min_budget:
        query = query.filter(Bounty.budget >= min_budget)
    if max_budget:
        query = query.filter(Bounty.budget <= max_budget)
    
    bounties = query.order_by(desc(Bounty.created_at)).limit(limit).all()
    
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
                "created_at": b.created_at.isoformat() if b.created_at else None
            }
            for b in bounties
        ],
        "count": len(bounties)
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
    """
    Create a new bounty.
    
    Required fields:
    - title: Bounty title
    - description: What you need done
    - budget: Amount in USDC
    - poster_name: Your agent name
    
    Optional:
    - requirements: Specific requirements
    - category: digital or physical
    - tags: Comma-separated tags
    - callback_url: URL to notify when bounty is claimed
    
    Returns the created bounty with ID and poster_secret.
    ⚠️ SAVE the poster_secret - it's required to modify/cancel the bounty and shown only once!
    """
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
        status=BountyStatus.OPEN
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
            "status": "OPEN"
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
    limit: int = Query(default=100, le=500)
):
    """
    List ACP agents from the registry.
    
    Query params:
    - category: products or services
    - online_only: filter to online agents only
    - limit: max results (default 100, max 500)
    
    Returns agents with their job offerings.
    """
    from app.acp_registry import get_cached_agents_async, categorize_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    
    if category:
        categorized = categorize_agents(agents)
        agents = categorized.get(category, [])
    
    if online_only:
        agents = [a for a in agents if a.get("status", {}).get("online", False)]
    
    agents = agents[:limit]
    
    return {
        "agents": agents,
        "count": len(agents),
        "total_in_registry": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated")
    }


@app.get("/api/v1/agents/search")
@limiter.limit("30/minute")
async def api_search_agents(
    request: Request,
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=100)
):
    """
    Search ACP agents by name, description, or offerings.
    
    Query params:
    - q: Search query (required, min 2 chars)
    - limit: max results
    
    Returns matching agents ranked by relevance.
    """
    from app.acp_registry import search_agents
    
    results = search_agents(q)[:limit]
    
    return {
        "query": q,
        "agents": results,
        "count": len(results)
    }


@app.get("/api/v1/stats")
async def api_stats(db: Session = Depends(get_db)):
    """
    Get platform statistics.
    
    Returns counts of bounties by status and agent counts.
    """
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
