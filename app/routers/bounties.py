"""API routes for bounty CRUD operations."""
import hashlib
import logging
from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.constants import (
    DEFAULT_PAGE_SIZE,
    ERR_BOUNTY_NOT_FOUND,
    ERR_INVALID_CALLBACK_URL,
    ERR_INVALID_SECRET,
    ERR_INVALID_STATUS,
    MAX_PAGE_SIZE,
)
from app.database import get_db
from app.models import Bounty, BountyStatus, verify_secret
from app.schemas import (
    ACPSearchResult,
    BountyCancel,
    BountyClaim,
    BountyClaimResponse,
    BountyCreate,
    BountyFulfill,
    BountyMatch,
    BountyPostResponse,
    BountyResponse,
    BountyUnclaim,
    EnvelopedBountyList,
    PaginationMeta,
)
from app.services.bounty_service import (
    cancel_bounty as svc_cancel_bounty,
    claim_bounty as svc_claim_bounty,
    create_bounty as svc_create_bounty,
    fulfill_bounty as svc_fulfill_bounty,
    get_bounty_by_id,
    search_acp_registry,
    send_bounty_webhook,
)
from app.utils import validate_callback_url

router = APIRouter(prefix="/api/v1/bounties", tags=["bounties"])
logger = logging.getLogger(__name__)


def _error(status: int, detail: str, code: str, request: Request) -> HTTPException:
    """Create a structured HTTPException with error code and request ID.

    Args:
        status: HTTP status code.
        detail: Human-readable error message.
        code: Machine-readable error code.
        request: The incoming request (for request_id).

    Returns:
        HTTPException with structured detail.
    """
    request_id = getattr(request.state, "request_id", "")
    raise HTTPException(
        status_code=status,
        detail={"detail": detail, "code": code, "request_id": request_id},
    )


@router.post(
    "/",
    response_model=BountyPostResponse,
    summary="Create a new bounty",
    description="Create a new bounty. First checks ACP registry for existing matches. Returns a poster_secret token — SAVE THIS!",
    response_description="Bounty creation result with optional ACP match.",
)
async def create_bounty(bounty: BountyCreate, request: Request, db: Session = Depends(get_db)) -> BountyPostResponse:
    """Create a new bounty, checking ACP first.

    Args:
        bounty: Bounty creation data.
        request: The incoming request.
        db: Database session.

    Returns:
        BountyPostResponse with the created bounty or ACP match.
    """
    if bounty.poster_callback_url and not validate_callback_url(bounty.poster_callback_url):
        _error(400, "Invalid callback URL: private/internal addresses are not allowed", ERR_INVALID_CALLBACK_URL, request)

    search_query = f"{bounty.title} {bounty.tags or ''}"
    acp_result = await search_acp_registry(search_query)

    if acp_result.found and len(acp_result.agents) > 0:
        return BountyPostResponse(
            bounty=None,
            poster_secret=None,
            acp_match=acp_result,
            action="acp_available",
            message=f"Service already available on ACP! Found {len(acp_result.agents)} matching agent(s).",
        )

    db_bounty, secret_token = svc_create_bounty(
        db,
        poster_name=bounty.poster_name,
        title=bounty.title,
        description=bounty.description,
        budget=bounty.budget,
        category=bounty.category,
        requirements=bounty.requirements,
        tags=bounty.tags,
        poster_callback_url=bounty.poster_callback_url,
        set_expiry=False,
    )

    return BountyPostResponse(
        bounty=BountyResponse.model_validate(db_bounty),
        poster_secret=secret_token,
        acp_match=acp_result,
        action="posted",
        message="Bounty posted! SAVE YOUR poster_secret — you need it to modify/cancel this bounty.",
    )


@router.get(
    "/",
    response_model=EnvelopedBountyList,
    summary="List bounties",
    description="List bounties with optional filters. Returns paginated results.",
    response_description="Paginated list of bounties.",
)
def list_bounties(
    request: Request,
    status: Optional[str] = None,
    category: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    search: Optional[str] = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, le=MAX_PAGE_SIZE),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List bounties with optional filters.

    Args:
        request: The incoming request.
        status: Filter by bounty status.
        category: Filter by category.
        min_budget: Minimum budget filter.
        max_budget: Maximum budget filter.
        search: Search term for title/description/tags.
        limit: Max results per page.
        offset: Offset for pagination.
        db: Database session.

    Returns:
        Enveloped bounty list with pagination metadata.
    """
    query = db.query(Bounty)
    if status:
        query = query.filter(Bounty.status == status)
    if category:
        query = query.filter(Bounty.category == category)
    if min_budget:
        query = query.filter(Bounty.budget >= min_budget)
    if max_budget:
        query = query.filter(Bounty.budget <= max_budget)
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Bounty.title.ilike(search_term))
            | (Bounty.description.ilike(search_term))
            | (Bounty.tags.ilike(search_term))
        )

    total = query.count()
    bounties = query.order_by(desc(Bounty.created_at)).offset(offset).limit(limit).all()
    page = (offset // limit) + 1 if limit > 0 else 1
    bounty_responses = [BountyResponse.model_validate(b) for b in bounties]

    return {
        "data": bounty_responses,
        "meta": PaginationMeta(total=total, page=page, per_page=limit),
        "bounties": bounty_responses,
        "total": total,
    }


@router.get(
    "/open",
    summary="List open bounties",
    description="List OPEN bounties available for claiming.",
    response_description="List of open bounties with count.",
)
def list_open_bounties(
    request: Request,
    category: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List OPEN bounties available for claiming.

    Args:
        request: The incoming request.
        category: Filter by category.
        min_budget: Minimum budget.
        max_budget: Maximum budget.
        limit: Max results.
        db: Database session.

    Returns:
        Dict with open_bounties list and count.
    """
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


@router.get(
    "/{bounty_id}",
    response_model=BountyResponse,
    summary="Get bounty by ID",
    description="Get a specific bounty by its ID. Includes ETag for caching.",
    response_description="Bounty details.",
)
def get_bounty(bounty_id: int, request: Request, db: Session = Depends(get_db)) -> Any:
    """Get a specific bounty by ID with ETag support.

    Args:
        bounty_id: The bounty ID.
        request: The incoming request.
        db: Database session.

    Returns:
        BountyResponse (via JSONResponse with ETag header).
    """
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        _error(404, "Bounty not found", ERR_BOUNTY_NOT_FOUND, request)

    response_data = BountyResponse.model_validate(bounty)
    # ETag based on status + updated_at
    etag_source = f"{bounty.id}-{bounty.status}-{bounty.updated_at or bounty.created_at}"
    etag = hashlib.md5(etag_source.encode()).hexdigest()

    # Check If-None-Match
    if_none_match = request.headers.get("If-None-Match")
    if if_none_match and if_none_match.strip('"') == etag:
        from fastapi.responses import Response
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=response_data.model_dump(mode="json"),
        headers={"ETag": f'"{etag}"'},
    )


@router.post(
    "/{bounty_id}/claim",
    response_model=BountyClaimResponse,
    summary="Claim a bounty",
    description="Claim a bounty as an agent willing to fulfill it. Returns a claimer_secret — SAVE THIS!",
    response_description="Claim confirmation with claimer_secret.",
)
async def claim_bounty(
    bounty_id: int,
    claim: BountyClaim,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> BountyClaimResponse:
    """Claim a bounty.

    Args:
        bounty_id: The bounty ID.
        claim: Claim data.
        request: The incoming request.
        background_tasks: Background task runner.
        db: Database session.

    Returns:
        BountyClaimResponse with the claimer_secret.
    """
    if claim.claimer_callback_url and not validate_callback_url(claim.claimer_callback_url):
        _error(400, "Invalid callback URL: private/internal addresses are not allowed", ERR_INVALID_CALLBACK_URL, request)

    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        _error(404, "Bounty not found", ERR_BOUNTY_NOT_FOUND, request)
    if bounty.status != BountyStatus.OPEN:
        _error(400, "Bounty is not available for claiming", ERR_INVALID_STATUS, request)

    secret_token = svc_claim_bounty(db, bounty, claim.claimer_name, claim.claimer_callback_url)

    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id, "title": bounty.title,
            "budget_usdc": bounty.budget, "claimed_by": claim.claimer_name, "status": "CLAIMED",
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.claimed", bounty_data)

    return BountyClaimResponse(
        bounty_id=bounty.id,
        claimed_by=claim.claimer_name,
        claimer_secret=secret_token,
        message="Bounty claimed! SAVE YOUR claimer_secret — you need it to unclaim.",
    )


@router.post(
    "/{bounty_id}/unclaim",
    response_model=BountyResponse,
    summary="Unclaim a bounty",
    description="Release a claim on a bounty back to OPEN status. Requires claimer_secret.",
    response_description="Updated bounty with OPEN status.",
)
async def unclaim_bounty(
    bounty_id: int,
    unclaim: BountyUnclaim,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Bounty:
    """Unclaim a bounty.

    Args:
        bounty_id: The bounty ID.
        unclaim: Unclaim data with claimer_secret.
        request: The incoming request.
        background_tasks: Background task runner.
        db: Database session.

    Returns:
        The updated bounty.
    """
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        _error(404, "Bounty not found", ERR_BOUNTY_NOT_FOUND, request)
    if not verify_secret(unclaim.claimer_secret, bounty.claimer_secret_hash):
        _error(403, "Invalid claimer_secret", ERR_INVALID_SECRET, request)
    if bounty.status != BountyStatus.CLAIMED:
        _error(400, "Bounty is not in CLAIMED status", ERR_INVALID_STATUS, request)

    bounty.status = BountyStatus.OPEN
    bounty.claimed_by = None
    bounty.claimer_callback_url = None
    bounty.claimer_secret_hash = None
    bounty.claimed_at = None
    db.commit()
    db.refresh(bounty)

    if bounty.poster_callback_url:
        bounty_data = {"id": bounty.id, "title": bounty.title, "budget_usdc": bounty.budget, "status": "OPEN"}
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.unclaimed", bounty_data)

    return bounty


@router.post(
    "/{bounty_id}/match",
    response_model=BountyResponse,
    summary="Match bounty to ACP service",
    description="Match a bounty to an ACP service. Requires poster_secret.",
    response_description="Updated bounty with MATCHED status.",
)
async def match_bounty(
    bounty_id: int,
    match: BountyMatch,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Bounty:
    """Match a bounty to an ACP service.

    Args:
        bounty_id: The bounty ID.
        match: Match data with poster_secret and ACP info.
        request: The incoming request.
        background_tasks: Background task runner.
        db: Database session.

    Returns:
        The updated bounty.
    """
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        _error(404, "Bounty not found", ERR_BOUNTY_NOT_FOUND, request)
    if not verify_secret(match.poster_secret, bounty.poster_secret_hash):
        _error(403, "Invalid poster_secret", ERR_INVALID_SECRET, request)
    if bounty.status not in [BountyStatus.OPEN, BountyStatus.CLAIMED]:
        _error(400, "Bounty is not available for matching", ERR_INVALID_STATUS, request)

    bounty.status = BountyStatus.MATCHED
    bounty.matched_service_id = match.service_id
    bounty.matched_acp_agent = match.acp_agent_wallet
    bounty.matched_acp_job = match.acp_job_offering
    bounty.matched_at = datetime.utcnow()
    db.commit()
    db.refresh(bounty)

    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id, "title": bounty.title, "budget_usdc": bounty.budget,
            "matched_acp_agent": bounty.matched_acp_agent,
            "matched_acp_job": bounty.matched_acp_job, "status": "MATCHED",
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.matched", bounty_data)

    return bounty


@router.post(
    "/{bounty_id}/fulfill",
    response_model=BountyResponse,
    summary="Fulfill a bounty",
    description="Mark bounty as fulfilled after ACP job completion. Requires poster_secret.",
    response_description="Updated bounty with FULFILLED status.",
)
async def fulfill_bounty(
    bounty_id: int,
    fulfill: BountyFulfill,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> Bounty:
    """Fulfill a bounty.

    Args:
        bounty_id: The bounty ID.
        fulfill: Fulfillment data with poster_secret and acp_job_id.
        request: The incoming request.
        background_tasks: Background task runner.
        db: Database session.

    Returns:
        The updated bounty.
    """
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        _error(404, "Bounty not found", ERR_BOUNTY_NOT_FOUND, request)
    if not verify_secret(fulfill.poster_secret, bounty.poster_secret_hash):
        _error(403, "Invalid poster_secret", ERR_INVALID_SECRET, request)
    if bounty.status not in [BountyStatus.MATCHED, BountyStatus.CLAIMED]:
        _error(400, "Bounty must be claimed or matched before fulfilling", ERR_INVALID_STATUS, request)

    svc_fulfill_bounty(db, bounty, fulfill.acp_job_id)

    bounty_data = {
        "id": bounty.id, "title": bounty.title,
        "budget_usdc": bounty.budget, "status": "FULFILLED", "acp_job_id": bounty.acp_job_id,
    }
    if bounty.poster_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.fulfilled", bounty_data)
    if bounty.claimer_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.claimer_callback_url, "bounty.fulfilled", bounty_data)

    return bounty


@router.post(
    "/{bounty_id}/cancel",
    response_model=BountyResponse,
    summary="Cancel a bounty",
    description="Cancel a bounty. Requires poster_secret.",
    response_description="Updated bounty with CANCELLED status.",
)
def cancel_bounty(bounty_id: int, cancel: BountyCancel, request: Request, db: Session = Depends(get_db)) -> Bounty:
    """Cancel a bounty.

    Args:
        bounty_id: The bounty ID.
        cancel: Cancel data with poster_secret.
        request: The incoming request.
        db: Database session.

    Returns:
        The updated bounty.
    """
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        _error(404, "Bounty not found", ERR_BOUNTY_NOT_FOUND, request)
    if not verify_secret(cancel.poster_secret, bounty.poster_secret_hash):
        _error(403, "Invalid poster_secret", ERR_INVALID_SECRET, request)
    if bounty.status == BountyStatus.FULFILLED:
        _error(400, "Cannot cancel fulfilled bounty", ERR_INVALID_STATUS, request)

    return svc_cancel_bounty(db, bounty)


@router.post(
    "/check-acp",
    response_model=ACPSearchResult,
    summary="Check ACP registry",
    description="Check ACP registry for existing services matching a query.",
    response_description="ACP search results.",
)
async def check_acp(query: str = Query(..., description="Search query for ACP registry")) -> ACPSearchResult:
    """Check ACP registry for matching services.

    Args:
        query: Search query string.

    Returns:
        ACPSearchResult with matching agents.
    """
    return await search_acp_registry(query)
