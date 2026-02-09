"""API v1 endpoints: agents and stats (bounty endpoints are in bounties.py)."""
import logging
from math import ceil
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.constants import AGENTS_DEFAULT_PAGE_SIZE, AGENTS_MAX_PAGE_SIZE
from app.database import get_db
from app.models import Bounty, BountyStatus

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["api_v1"])


# --------------- Agent endpoints ---------------


@router.get(
    "/agents",
    summary="List ACP agents",
    description="List ACP agents from the registry with optional category and online filters.",
    response_description="Paginated list of ACP agents.",
)
async def api_list_agents(
    request: Request,
    category: Optional[str] = None,
    online_only: bool = False,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=AGENTS_DEFAULT_PAGE_SIZE, le=AGENTS_MAX_PAGE_SIZE),
) -> dict[str, Any]:
    """List ACP agents from the registry.

    Args:
        request: The incoming request.
        category: Filter by agent category ('products' or 'services').
        online_only: Only show online agents.
        page: Page number.
        limit: Results per page.

    Returns:
        Dict with agents list and pagination metadata.
    """
    from app.acp_registry import categorize_agents, get_cached_agents_async

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
    agents_page = agents[start: start + limit]

    return {
        "data": agents_page,
        "meta": {"total": total, "page": page, "per_page": limit},
        # Backward compat
        "agents": agents_page,
        "count": len(agents_page),
        "total_in_registry": len(cache.get("agents", [])),
        "last_updated": cache.get("last_updated"),
        "page": page,
        "per_page": limit,
        "total_pages": total_pages,
        "has_next": page < total_pages,
    }


@router.get(
    "/agents/search",
    summary="Search ACP agents",
    description="Search ACP agents by name, description, or offerings.",
    response_description="Search results with matching agents.",
)
async def api_search_agents(
    request: Request,
    q: str = Query(..., min_length=2),
    limit: int = Query(default=20, le=100),
) -> dict[str, Any]:
    """Search ACP agents by name, description, or offerings.

    Args:
        request: The incoming request.
        q: Search query (min 2 chars).
        limit: Max results.

    Returns:
        Dict with query, agents, and count.
    """
    from app.acp_registry import search_agents

    results = search_agents(q)[:limit]
    return {"query": q, "agents": results, "count": len(results)}


@router.get(
    "/stats",
    summary="Platform statistics",
    description="Get platform statistics including bounty counts and agent registry info.",
    response_description="Platform statistics.",
)
async def api_stats(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Get platform statistics.

    Args:
        db: Database session.

    Returns:
        Dict with bounty and agent statistics.
    """
    from app.acp_registry import categorize_agents, get_cached_agents_async

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
