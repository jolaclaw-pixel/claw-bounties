"""Web (HTML) routes for the Claw Bounties frontend."""
import os
import httpx
import logging
from datetime import datetime, timedelta
from math import ceil
from typing import Optional, Any

from fastapi import APIRouter, Depends, Form, Request, BackgroundTasks, Query
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bounty, BountyStatus, Service, generate_secret, verify_secret

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
    recent_bounties = (
        db.query(Bounty)
        .filter(Bounty.status == BountyStatus.OPEN)
        .order_by(desc(Bounty.created_at))
        .limit(6)
        .all()
    )
    stats = {
        "total_bounties": db.query(Bounty).count(),
        "open_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.OPEN).count(),
        "matched_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.MATCHED).count(),
        "fulfilled_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count(),
    }
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
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

    matching_services: list[Service] = []
    if bounty.tags:
        tags = bounty.tags.split(",")
        for tag in tags:
            matching = (
                db.query(Service)
                .filter(Service.is_active == True, Service.tags.ilike(f"%{tag.strip()}%"))
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
    if claimer_callback_url:
        from app.utils import validate_callback_url

        if not validate_callback_url(claimer_callback_url):
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "error": "Invalid callback URL: private/internal addresses are not allowed."},
                status_code=400,
            )

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

    if bounty.poster_callback_url:
        from app.utils import validate_callback_url as _validate

        if _validate(bounty.poster_callback_url):

            async def send_notification() -> None:
                try:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(
                            bounty.poster_callback_url,
                            json={
                                "event": "bounty.claimed",
                                "bounty": {
                                    "id": bounty.id,
                                    "title": bounty.title,
                                    "budget_usdc": bounty.budget,
                                    "claimed_by": claimer_name,
                                    "status": "CLAIMED",
                                },
                            },
                        )
                except Exception as e:
                    logger.error(f"Webhook failed: {e}")

            background_tasks.add_task(send_notification)

    return RedirectResponse(url=f"/bounties/{bounty_id}", status_code=303)


@router.post("/bounties/{bounty_id}/fulfill")
async def web_fulfill_bounty(
    request: Request,
    bounty_id: int,
    background_tasks: BackgroundTasks,
    poster_secret: str = Form(...),
    db: Session = Depends(get_db),
) -> Any:
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
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

    bounty.status = BountyStatus.FULFILLED
    bounty.fulfilled_at = datetime.utcnow()
    db.commit()

    bounty_data = {"id": bounty.id, "title": bounty.title, "budget_usdc": bounty.budget, "status": "FULFILLED"}

    async def send_notifications() -> None:
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


@router.get("/services")
async def services_page(
    request: Request,
    category: Optional[str] = None,
    search: Optional[str] = None,
    page: int = 1,
    db: Session = Depends(get_db),
) -> Any:
    query = db.query(Service).filter(Service.is_active == True)
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
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)
    return templates.TemplateResponse("service_detail.html", {"request": request, "service": service})


@router.get("/post-bounty")
async def post_bounty_form(request: Request) -> Any:
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
    if poster_callback_url:
        from app.utils import validate_callback_url

        if not validate_callback_url(poster_callback_url):
            return templates.TemplateResponse(
                "error.html",
                {"request": request, "error": "Invalid callback URL: private/internal addresses are not allowed."},
                status_code=400,
            )

    from app.routers.bounties import search_acp_registry

    search_query = f"{title} {tags or ''}"
    acp_result = await search_acp_registry(search_query)

    if acp_result.found and len(acp_result.agents) > 0:
        return templates.TemplateResponse(
            "acp_found.html",
            {"request": request, "title": title, "description": description, "budget": budget, "acp_result": acp_result},
        )

    secret_token, secret_hash = generate_secret()

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
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)

    return templates.TemplateResponse(
        "bounty_created.html", {"request": request, "bounty": bounty, "poster_secret": secret_token}
    )


@router.get("/list-service")
async def list_service_form(request: Request) -> Any:
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
    from app.routers.services import _auto_match_bounties

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
        acp_job_offering=acp_job_offering if acp_job_offering else None,
    )
    db.add(service)
    db.commit()
    db.refresh(service)

    if acp_agent_wallet and acp_job_offering:
        _auto_match_bounties(db, service)

    return templates.TemplateResponse(
        "service_created.html", {"request": request, "service": service, "agent_secret": secret_token}
    )


@router.get("/docs")
async def docs_page(request: Request) -> Any:
    return templates.TemplateResponse("docs.html", {"request": request})


@router.get("/success-stories")
async def success_stories_page(request: Request, db: Session = Depends(get_db)) -> Any:
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
    return templates.TemplateResponse("offline.html", {"request": request})


@router.get("/registry")
async def registry_page(request: Request, q: Optional[str] = None, page: int = 1) -> Any:
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
    from app.acp_registry import get_cached_agents_async

    cache = await get_cached_agents_async()
    agents = cache.get("agents", [])
    agent = next((a for a in agents if a.get("id") == agent_id), None)

    if not agent:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

    return templates.TemplateResponse("agent_detail.html", {"request": request, "agent": agent})
