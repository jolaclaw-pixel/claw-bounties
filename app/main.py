import os
from fastapi import FastAPI, Request, Depends, Form, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from dotenv import load_dotenv

from app.database import init_db, get_db
from app.models import Bounty, Service, BountyStatus
from app.routers import bounties, services

load_dotenv()

app = FastAPI(
    title="Claw Bounties",
    description="A bounty marketplace for Claw Agents",
    version="0.1.0"
)

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
    
    # No ACP match - post the bounty
    bounty = Bounty(
        poster_name=poster_name,
        poster_callback_url=poster_callback_url,
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
    return RedirectResponse(url=f"/bounties/{bounty.id}", status_code=303)


@app.get("/list-service")
async def list_service_form(request: Request):
    """Form to list a new service."""
    return templates.TemplateResponse("list_service.html", {"request": request})


@app.post("/list-service")
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
    
    service = Service(
        agent_name=agent_name,
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
    
    return RedirectResponse(url=f"/services/{service.id}", status_code=303)


@app.get("/registry")
async def registry_page(request: Request):
    """Browse the Virtuals ACP Registry - all agents, products, and services."""
    from app.acp_registry import get_cached_agents_async, categorize_agents
    
    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    last_updated = cache.get("last_updated")
    error = cache.get("error")
    
    categorized = categorize_agents(agents)
    
    return templates.TemplateResponse("registry.html", {
        "request": request,
        "products": categorized["products"],
        "services": categorized["services"],
        "total_agents": len(agents),
        "last_updated": last_updated,
        "error": error
    })


@app.post("/api/registry/refresh")
async def refresh_registry():
    """Manually refresh the ACP registry cache."""
    from app.acp_registry import refresh_cache
    
    cache = await refresh_cache()
    return {
        "status": "refreshed",
        "agents_count": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated")
    }


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


# ============ Agent API v1 ============
# Clean JSON endpoints for Claw agents to consume

@app.get("/api/v1/bounties")
async def api_list_bounties(
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
                "status": b.status.value if b.status else "OPEN",
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
async def api_open_bounties(
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
async def api_get_bounty(bounty_id: int, db: Session = Depends(get_db)):
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
            "status": bounty.status.value if bounty.status else "OPEN",
            "poster_name": bounty.poster_name,
            "poster_callback_url": bounty.poster_callback_url,
            "matched_acp_agent": bounty.matched_acp_agent,
            "matched_acp_job": bounty.matched_acp_job,
            "matched_at": bounty.matched_at.isoformat() if bounty.matched_at else None,
            "created_at": bounty.created_at.isoformat() if bounty.created_at else None
        }
    }


@app.post("/api/v1/bounties")
async def api_create_bounty(
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
    
    Returns the created bounty with ID.
    """
    bounty = Bounty(
        poster_name=poster_name,
        poster_callback_url=callback_url,
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
        }
    }


@app.get("/api/v1/agents")
async def api_list_agents(
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
async def api_search_agents(
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
