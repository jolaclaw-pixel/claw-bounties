"""API routes for bounty CRUD operations."""
import logging
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import desc

from app.database import get_db
from app.models import Bounty, BountyStatus, verify_secret
from app.schemas import (
    BountyCreate, BountyResponse, BountyList,
    BountyClaim, BountyClaimResponse, BountyMatch, BountyUnclaim,
    BountyFulfill, BountyCancel, BountyPostResponse,
    ACPSearchResult,
)
from app.services.bounty_service import (
    search_acp_registry,
    send_bounty_webhook,
    get_bounty_by_id,
    create_bounty as svc_create_bounty,
    claim_bounty as svc_claim_bounty,
    fulfill_bounty as svc_fulfill_bounty,
    cancel_bounty as svc_cancel_bounty,
)
from app.utils import validate_callback_url

router = APIRouter(prefix="/api/v1/bounties", tags=["bounties"])
logger = logging.getLogger(__name__)


@router.post("/", response_model=BountyPostResponse)
async def create_bounty(bounty: BountyCreate, db: Session = Depends(get_db)):
    """
    Create a new bounty.
    First checks ACP registry — if matching service exists, returns that instead.
    Returns a poster_secret token — SAVE THIS! Required to modify/cancel the bounty.
    """
    if bounty.poster_callback_url and not validate_callback_url(bounty.poster_callback_url):
        raise HTTPException(status_code=400, detail="Invalid callback URL: private/internal addresses are not allowed")

    search_query = f"{bounty.title} {bounty.tags or ''}"
    acp_result = await search_acp_registry(search_query)

    if acp_result.found and len(acp_result.agents) > 0:
        return BountyPostResponse(
            bounty=None,
            poster_secret=None,
            acp_match=acp_result,
            action="acp_available",
            message=f"Service already available on ACP! Found {len(acp_result.agents)} matching agent(s). Use ACP to fulfill your request directly.",
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
        message="Bounty posted! SAVE YOUR poster_secret — you need it to modify/cancel this bounty. No matching service found on ACP yet.",
    )


@router.get("/", response_model=BountyList)
def list_bounties(
    status: Optional[str] = None,
    category: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=100),
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List bounties with optional filters."""
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
    return BountyList(bounties=bounties, total=total)


@router.get("/open")
def list_open_bounties(
    category: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    limit: int = Query(default=50, le=100),
    db: Session = Depends(get_db),
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
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in bounties_list
        ],
        "count": len(bounties_list),
    }


@router.get("/{bounty_id}", response_model=BountyResponse)
def get_bounty(bounty_id: int, db: Session = Depends(get_db)):
    """Get a specific bounty by ID."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    return bounty


@router.post("/{bounty_id}/claim", response_model=BountyClaimResponse)
async def claim_bounty(
    bounty_id: int,
    claim: BountyClaim,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """
    Claim a bounty as an agent willing to fulfill it.
    Returns a claimer_secret token — SAVE THIS!
    """
    if claim.claimer_callback_url and not validate_callback_url(claim.claimer_callback_url):
        raise HTTPException(status_code=400, detail="Invalid callback URL: private/internal addresses are not allowed")

    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if bounty.status != BountyStatus.OPEN:
        raise HTTPException(status_code=400, detail="Bounty is not available for claiming")

    secret_token = svc_claim_bounty(db, bounty, claim.claimer_name, claim.claimer_callback_url)

    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id,
            "title": bounty.title,
            "budget_usdc": bounty.budget,
            "claimed_by": claim.claimer_name,
            "status": "CLAIMED",
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.claimed", bounty_data)

    return BountyClaimResponse(
        bounty_id=bounty.id,
        claimed_by=claim.claimer_name,
        claimer_secret=secret_token,
        message=f"Bounty claimed! SAVE YOUR claimer_secret — you need it to unclaim. Poster {'will be' if bounty.poster_callback_url else 'was NOT'} notified.",
    )


@router.post("/{bounty_id}/unclaim", response_model=BountyResponse)
async def unclaim_bounty(
    bounty_id: int,
    unclaim: BountyUnclaim,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Unclaim a bounty (release claim back to OPEN status). Requires claimer_secret."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if not verify_secret(unclaim.claimer_secret, bounty.claimer_secret_hash):
        raise HTTPException(status_code=403, detail="Invalid claimer_secret. Only the claimer can unclaim it.")
    if bounty.status != BountyStatus.CLAIMED:
        raise HTTPException(status_code=400, detail="Bounty is not in CLAIMED status")

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


@router.post("/{bounty_id}/match", response_model=BountyResponse)
async def match_bounty(
    bounty_id: int,
    match: BountyMatch,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Match a bounty to an ACP service. Requires poster_secret."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if not verify_secret(match.poster_secret, bounty.poster_secret_hash):
        raise HTTPException(status_code=403, detail="Invalid poster_secret. Only the bounty poster can match it to a service.")
    if bounty.status not in [BountyStatus.OPEN, BountyStatus.CLAIMED]:
        raise HTTPException(status_code=400, detail="Bounty is not available for matching")

    bounty.status = BountyStatus.MATCHED
    bounty.matched_service_id = match.service_id
    bounty.matched_acp_agent = match.acp_agent_wallet
    bounty.matched_acp_job = match.acp_job_offering
    bounty.matched_at = datetime.utcnow()
    db.commit()
    db.refresh(bounty)

    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id,
            "title": bounty.title,
            "budget_usdc": bounty.budget,
            "matched_acp_agent": bounty.matched_acp_agent,
            "matched_acp_job": bounty.matched_acp_job,
            "status": "MATCHED",
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.matched", bounty_data)

    return bounty


@router.post("/{bounty_id}/fulfill", response_model=BountyResponse)
async def fulfill_bounty(
    bounty_id: int,
    fulfill: BountyFulfill,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Mark bounty as fulfilled after ACP job completion. Requires poster_secret."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if not verify_secret(fulfill.poster_secret, bounty.poster_secret_hash):
        raise HTTPException(status_code=403, detail="Invalid poster_secret. Only the bounty poster can fulfill it.")
    if bounty.status not in [BountyStatus.MATCHED, BountyStatus.CLAIMED]:
        raise HTTPException(status_code=400, detail="Bounty must be claimed or matched before fulfilling")

    svc_fulfill_bounty(db, bounty, fulfill.acp_job_id)

    bounty_data = {
        "id": bounty.id,
        "title": bounty.title,
        "budget_usdc": bounty.budget,
        "status": "FULFILLED",
        "acp_job_id": bounty.acp_job_id,
    }
    if bounty.poster_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.fulfilled", bounty_data)
    if bounty.claimer_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.claimer_callback_url, "bounty.fulfilled", bounty_data)

    return bounty


@router.post("/{bounty_id}/cancel", response_model=BountyResponse)
def cancel_bounty(bounty_id: int, cancel: BountyCancel, db: Session = Depends(get_db)):
    """Cancel a bounty. Requires poster_secret."""
    bounty = get_bounty_by_id(db, bounty_id)
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if not verify_secret(cancel.poster_secret, bounty.poster_secret_hash):
        raise HTTPException(status_code=403, detail="Invalid poster_secret. Only the bounty poster can cancel it.")
    if bounty.status == BountyStatus.FULFILLED:
        raise HTTPException(status_code=400, detail="Cannot cancel fulfilled bounty")

    return svc_cancel_bounty(db, bounty)


@router.post("/check-acp", response_model=ACPSearchResult)
async def check_acp(query: str = Query(..., description="Search query for ACP registry")):
    """Check ACP registry for existing services matching a query."""
    return await search_acp_registry(query)
