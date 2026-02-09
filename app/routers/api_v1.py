"""API v1 endpoints: agents and stats (bounty endpoints are in bounties.py)."""
import logging
from math import ceil
from typing import Optional, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Bounty, BountyStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["api_v1"])


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
