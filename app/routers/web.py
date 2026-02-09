"""Web (HTML) routes for the Claw Bounties frontend."""
import logging
from datetime import datetime
from math import ceil
from typing import Optional, Any

from fastapi import APIRouter, Depends, Form, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bounty, BountyStatus, Service, verify_secret
from app.services.bounty_service import (
    create_bounty,
    claim_bounty,
    fulfill_bounty,
    get_bounty_by_id,
    get_platform_stats,
    search_acp_registry,
    send_bounty_webhook,
)
from app.services.service_service import create_service, auto_match_bounties
from app.utils import validate_callback_url

logger = logging.getLogger(__name__)
router = APIRouter(tags=["web"])

templates = Jinja2Templates(directory="templates")


def get_agent_count() -> int:
    """Get the current agent count from the ACP cache."""
    try:
        from app.acp_registry import get_cached_agents
        cache = get_cached_agents()
        count = len(cache.get("agents", []))
        return count if count > 0 else 1400
    except Exception:
        return 1400


@router.get("/")
async def home(request: Request, db: Session = Depends(get_db)) -> Any:
    """Landing page with recent bounties and platform stats."""
    recent_bounties = (
        db.query(Bounty)
        .filter(Bounty.status == BountyStatus.OPEN)
        .order_by(desc(Bounty.created_at))
        .limit(6)
        .all()
    )
    stats = get_platform_stats(db)
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "bounties": recent_bounties, "stats": stats, "agent_count": get_agent_count()},
    )


@router.get("/bounties")
async def bounties_page(
    request: Request,
    status: Optional[str] = None,
    category: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> Any:
    """Paginated bounties listing with filters."""
    query = db.query(Bounty)
    if status:
        query = query.filter(Bounty.status == status)
    if category:
        query = query.filter(Bounty.category == category)
    if search:
        search_term = f"%{search}%"
        query = query.filter((Bounty.title.ilike(search_term)) | (Bounty.description.ilike(search_term)))

    total = query.count()
    per_page = 12
    bounties_list = query.order_by(desc(Bounty.created_at)).offset((page - 1) * per_page).limit(per_page).all()

    return templates.TemplateResponse(
        "bounties.html",
        {
            "request": request,
            "bounties": bounties_list,
            "total": total,
            "page": page,
            "pages": (total + per_page - 1) // per_page,
            "status": status,
            "category": category,
            "search": search,
        },
    )


@router.get("/bounties/{bounty_id}")
async def bounty_detail(request: Request, bounty_id: int, db: Session = Depends(get_db)) -> Any:
    """Bounty detail page with matching services and ACP agents."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

    matching_services: list[Service] = []
    if bounty.tags:
        tags = bounty.tags.split(",")
        for tag in tags:
            matching = (
                db.query(Service)
                .filter(Service.is_active, Service.tags.ilike(f"%{tag.strip()}%"))
                .limit(3)
                .all()
            )
            matching_services.extend(matching)

    expires_in_days = None
    if bounty.expires_at:
        delta = bounty.expires_at - datetime.utcnow()
        expires_in_days = max(0, delta.days)

    matching_agents: list[dict[str, Any]] = []
    try:
        from app.acp_registry import search_agents as _search_acp

        search_terms = bounty.title
        if bounty.tags:
            search_terms += " " + bounty.tags.replace(",", " ")
        matching_agents = _search_acp(search_terms)[:5]
    except Exception:
        pass

    return templates.TemplateResponse(
        "bounty_detail.html",
        {
            "request": request,
            "bounty": bounty,
            "matching_services": list(set(matching_services))[:6],
            "expires_in_days": expires_in_days,
            "matching_agents": matching_agents,
        },
    )


@router.post("/bounties/{bounty_id}/claim")
async def web_claim_bounty(
    request: Request,
    bounty_id: int,
    background_tasks: BackgroundTasks,
    claimer_name: str = Form(...),
    claimer_callback_url: str = Form(None),
    db: Session = Depends(get_db),
) -> Any:
    """Handle web form bounty claiming."""
    if claimer_callback_url and not validate_callback_url(claimer_callback_url):
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Invalid callback URL: private/internal addresses are not allowed."},
            status_code=400,
        )

    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    if bounty.status != BountyStatus.OPEN:
        return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)

    claim_bounty(db, bounty, claimer_name, claimer_callback_url)

    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id,
            "title": bounty.title,
            "budget_usdc": bounty.budget,
            "claimed_by": claimer_name,
            "status": "CLAIMED",
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.claimed", bounty_data)

    return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)


@router.post("/bounties/{bounty_id}/fulfill")
async def web_fulfill_bounty(
    request: Request,
    bounty_id: int,
    background_tasks: BackgroundTasks,
    poster_secret: str = Form(...),
    db: Session = Depends(get_db),
) -> Any:
    """Handle web form bounty fulfillment."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

    if not verify_secret(poster_secret, bounty.poster_secret_hash):
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Invalid poster_secret. Only the bounty poster can mark it as fulfilled."},
            status_code=403,
        )

    if bounty.status not in [BountyStatus.CLAIMED, BountyStatus.MATCHED]:
        return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)

    fulfill_bounty(db, bounty)

    bounty_data = {"id": bounty.id, "title": bounty.title, "budget_usdc": bounty.budget, "status": "FULFILLED"}
    if bounty.poster_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.fulfilled", bounty_data)
    if bounty.claimer_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.claimer_callback_url, "bounty.fulfilled", bounty_data)

    return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)


@router.get("/services")
async def services_page(
    request: Request,
    category: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> Any:
    """Paginated services listing with filters."""
    query = db.query(Service).filter(Service.is_active)
    if category:
        query = query.filter(Service.category == category)
    if search:
        search_term = f"%{search}%"
        query = query.filter((Service.name.ilike(search_term)) | (Service.description.ilike(search_term)))

    total = query.count()
    per_page = 12
    services_list = query.order_by(desc(Service.created_at)).offset((page - 1) * per_page).limit(per_page).all()

    return templates.TemplateResponse(
        "services.html",
        {
            "request": request,
            "services": services_list,
            "total": total,
            "page": page,
            "pages": (total + per_page - 1) // per_page,
            "category": category,
            "search": search,
        },
    )


@router.get("/services/{service_id}")
async def service_detail(request: Request, service_id: int, db: Session = Depends(get_db)) -> Any:
    """Service detail page."""
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("service_detail.html", {"request": request, "service": service})


@router.get("/post-bounty")
async def post_bounty_form(request: Request) -> Any:
    """Render the post-bounty form."""
    return templates.TemplateResponse("post_bounty.html", {"request": request})


@router.post("/post-bounty")
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
    db: Session = Depends(get_db),
) -> Any:
    """Handle web form bounty creation — checks ACP first, then posts."""
    if poster_callback_url and not validate_callback_url(poster_callback_url):
        return templates.TemplateResponse(
            "error.html",
            {"request": request, "error": "Invalid callback URL: private/internal addresses are not allowed."},
            status_code=400,
        )

    search_query = f"{title} {tags or ''}"
    acp_result = await search_acp_registry(search_query)

    if acp_result.found and len(acp_result.agents) > 0:
        return templates.TemplateResponse(
            "acp_found.html",
            {"request": request, "title": title, "description": description, "budget": budget, "acp_result": acp_result},
        )

    bounty, secret_token = create_bounty(
        db,
        poster_name=poster_name,
        title=title,
        description=description,
        budget=budget,
        category=category,
        requirements=requirements,
        tags=tags,
        poster_callback_url=poster_callback_url,
        set_expiry=True,
    )

    return templates.TemplateResponse(
        "bounty_created.html", {"request": request, "bounty": bounty, "poster_secret": secret_token}
    )


@router.get("/list-service")
async def list_service_form(request: Request) -> Any:
    """Render the list-service form."""
    return templates.TemplateResponse("list_service.html", {"request": request})


@router.post("/list-service")
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
    db: Session = Depends(get_db),
) -> Any:
    """Handle web form service listing creation."""
    service, secret_token = create_service(
        db,
        agent_name=agent_name,
        name=name,
        description=description,
        price=price,
        category=category,
        location=location,
        shipping_available=shipping_available == "on",
        tags=tags,
        acp_agent_wallet=acp_agent_wallet if acp_agent_wallet else None,
        acp_job_offering=acp_job_offering if acp_job_offering else None,
    )

    if acp_agent_wallet and acp_job_offering:
        auto_match_bounties(db, service)

    return templates.TemplateResponse(
        "service_created.html", {"request": request, "service": service, "agent_secret": secret_token}
    )


@router.get("/docs")
async def docs_page(request: Request) -> Any:
    """API documentation page."""
    return templates.TemplateResponse("docs.html", {"request": request})


@router.get("/success-stories")
async def success_stories_page(request: Request, db: Session = Depends(get_db)) -> Any:
    """Success stories page showing fulfilled bounties."""
    fulfilled_bounties = (
        db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).order_by(desc(Bounty.fulfilled_at)).limit(20).all()
    )
    total_bounties = db.query(Bounty).count()
    fulfilled_count = db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count()
    total_value = db.query(func.sum(Bounty.budget)).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    unique_posters = (
        db.query(func.count(func.distinct(Bounty.poster_name))).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    )
    unique_claimers = (
        db.query(func.count(func.distinct(Bounty.claimed_by))).filter(Bounty.status == BountyStatus.FULFILLED).scalar() or 0
    )

    return templates.TemplateResponse(
        "success_stories.html",
        {
            "request": request,
            "stories": fulfilled_bounties,
            "total_bounties": total_bounties,
            "fulfilled_count": fulfilled_count,
            "total_value": int(total_value),
            "unique_agents": unique_posters + unique_claimers,
        },
    )


@router.get("/offline.html")
async def offline_page(request: Request) -> Any:
    """Offline fallback page for PWA."""
    return templates.TemplateResponse("offline.html", {"request": request})


@router.get("/registry")
async def registry_page(request: Request, q: Optional[str] = None, page: int = 1) -> Any:
    """ACP agent registry browser with search and pagination."""
    from app.acp_registry import get_cached_agents_async, categorize_agents, search_agents

    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    last_updated = cache.get("last_updated")
    error = cache.get("error")

    if q and q.strip():
        agents = search_agents(q)

    total_agents_count = len(agents)
    online_count = sum(1 for a in agents if a.get("status", {}).get("online", False))

    per_page = 50
    total_pages = max(1, ceil(total_agents_count / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    agents_page = agents[start : start + per_page]
    categorized = categorize_agents(agents_page)

    return templates.TemplateResponse(
        "registry.html",
        {
            "request": request,
            "products": categorized["products"],
            "services": categorized["services"],
            "total_agents": total_agents_count,
            "online_count": online_count,
            "last_updated": last_updated,
            "error": error,
            "query": q,
            "page": page,
            "total_pages": total_pages,
        },
    )


@router.get("/agents/{agent_id}")
async def agent_detail_page(request: Request, agent_id: int) -> Any:
    """Individual ACP agent detail page."""
    from app.acp_registry import get_cached_agents_async

    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    agent = next((a for a in agents if a.get("id") == agent_id), None)

    if not agent:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

    return templates.TemplateResponse("agent_detail.html", {"request": request, "agent": agent})
