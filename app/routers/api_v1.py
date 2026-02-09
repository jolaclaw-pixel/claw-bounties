"""API v1 endpoints: agents, stats, and v1-specific bounty list/open/get/create."""
import logging
from math import ceil
from datetime import datetime, timedelta
from typing import Optional, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models import Bounty, BountyStatus, generate_secret
from app.schemas import BountyCreate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["api_v1"])


# --------------- Bounty endpoints (v1 format) ---------------

@router.get("/bounties")
async def api_list_bounties(
    request: Request,
    status: Optional[str] = None,
    category: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
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
                "status": b.status.value if hasattr(b.status, "value") else (b.status or "OPEN"),
                "poster_name": b.poster_name,
                "poster_callback_url": b.poster_callback_url,
                "matched_acp_agent": b.matched_acp_agent,
                "matched_acp_job": b.matched_acp_job,
                "expires_at": b.expires_at.isoformat() if b.expires_at else None,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in bounties_list
        ],
        "count": len(bounties_list),
    }


@router.get("/bounties/open")
async def api_open_bounties(
    request: Request,
    category: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    limit: int = Query(default=50, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
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
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in bounties_list
        ],
        "count": len(bounties_list),
    }


@router.get("/bounties/{bounty_id}")
async def api_get_bounty(
    request: Request, bounty_id: int, db: Session = Depends(get_db)
) -> dict[str, Any]:
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
            "status": bounty.status.value if hasattr(bounty.status, "value") else (bounty.status or "OPEN"),
            "poster_name": bounty.poster_name,
            "poster_callback_url": bounty.poster_callback_url,
            "matched_acp_agent": bounty.matched_acp_agent,
            "matched_acp_job": bounty.matched_acp_job,
            "matched_at": bounty.matched_at.isoformat() if bounty.matched_at else None,
            "expires_at": bounty.expires_at.isoformat() if bounty.expires_at else None,
            "created_at": bounty.created_at.isoformat() if bounty.created_at else None,
        }
    }


@router.post("/bounties")
async def api_create_bounty(
    request: Request,
    bounty_data: BountyCreate,
    db: Session = Depends(get_db),
) -> Any:
    """Create a new bounty via JSON body."""
    if bounty_data.poster_callback_url:
        from app.utils import validate_callback_url
        if not validate_callback_url(bounty_data.poster_callback_url):
            return JSONResponse(
                status_code=400,
                content={"error": "Invalid callback URL: private/internal addresses are not allowed"},
            )

    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = (
        db.query(Bounty)
        .filter(Bounty.poster_name == bounty_data.poster_name, Bounty.created_at >= one_hour_ago)
        .count()
    )
    if recent_count >= 5:
        return JSONResponse(
            status_code=429,
            content={
                "error": f"Rate limit exceeded: {bounty_data.poster_name} has created {recent_count} bounties in the last hour. Max 5 per hour."
            },
        )

    secret_token, secret_hash = generate_secret()

    bounty = Bounty(
        poster_name=bounty_data.poster_name,
        poster_callback_url=bounty_data.poster_callback_url,
        poster_secret_hash=secret_hash,
        title=bounty_data.title,
        description=bounty_data.description,
        requirements=bounty_data.requirements,
        budget=bounty_data.budget,
        category=bounty_data.category,
        tags=bounty_data.tags,
        status=BountyStatus.OPEN,
        expires_at=datetime.utcnow() + timedelta(days=30),
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
            "expires_at": bounty.expires_at.isoformat() if bounty.expires_at else None,
        },
        "poster_secret": secret_token,
        "message": "⚠️ SAVE your poster_secret! You need it to modify/cancel this bounty. It will NOT be shown again.",
    }


# --------------- Agent endpoints ---------------

@router.get("/agents")
async def api_list_agents(
    request: Request,
    category: Optional[str] = None,
    online_only: bool = False,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=100, le=500),
) -> dict[str, Any]:
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
    agents_page = agents[start : start + limit]

    return {
        "agents": agents_page,
        "count": len(agents_page),
        "total_in_registry": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated"),
        "page": page,
        "per_page": limit,
        "total_pages": total_pages,
        "has_next": page < total_pages,
    }


@router.get("/agents/search")
async def api_search_agents(
    request: Request,
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=100),
) -> dict[str, Any]:
    """Search ACP agents by name, description, or offerings."""
    from app.acp_registry import search_agents

    results = search_agents(q)[:limit]
    return {"query": q, "agents": results, "count": len(results)}


@router.get("/stats")
async def api_stats(db: Session = Depends(get_db)) -> dict[str, Any]:
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
            "fulfilled": db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count(),
        },
        "agents": {
            "total": len(agents),
            "products": len(categorized.get("products", [])),
            "services": len(categorized.get("services", [])),
        },
        "last_registry_update": cache.get("last_updated"),
    }
