from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Form
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime
import subprocess
import json
import os
import httpx
import logging

from app.database import get_db
from app.models import Bounty, BountyStatus, Service, generate_secret, verify_secret
from app.schemas import (
    BountyCreate, BountyResponse, BountyList, 
    BountyMatch, BountyFulfill, BountyCancel, BountyPostResponse,
    ACPSearchResult, ACPAgent
)

router = APIRouter(prefix="/api/bounties", tags=["bounties"])
logger = logging.getLogger(__name__)


async def send_bounty_webhook(callback_url: str, event: str, bounty_data: dict):
    """Send webhook notification for bounty events."""
    if not callback_url:
        return
    
    payload = {
        "event": event,
        "bounty": bounty_data,
        "timestamp": datetime.utcnow().isoformat()
    }
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(callback_url, json=payload)
            logger.info(f"Webhook sent ({event}) to {callback_url}: {response.status_code}")
    except Exception as e:
        logger.error(f"Webhook failed for {callback_url}: {e}")

# Path to ACP skill for registry scanning
ACP_SKILL_PATH = os.getenv("ACP_SKILL_PATH", "/Users/ethermage/.openclaw/virtuals-protocol-acp")


async def search_acp_registry(query: str) -> ACPSearchResult:
    """Search ACP registry for matching agents/services."""
    try:
        result = subprocess.run(
            ["npx", "tsx", "scripts/index.ts", "browse_agents", query],
            cwd=ACP_SKILL_PATH,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            return ACPSearchResult(found=False, agents=[], message=f"ACP search failed: {result.stderr}")
        
        data = json.loads(result.stdout)
        
        if not data or len(data) == 0:
            return ACPSearchResult(found=False, agents=[], message="No matching services found on ACP")
        
        agents = []
        for agent in data:
            job_offerings = [j.get("name", "") for j in agent.get("jobOfferings", [])]
            agents.append(ACPAgent(
                wallet_address=agent.get("walletAddress", ""),
                name=agent.get("name", "Unknown"),
                description=agent.get("description", ""),
                job_offerings=job_offerings
            ))
        
        return ACPSearchResult(
            found=True,
            agents=agents,
            message=f"Found {len(agents)} matching service(s) on ACP"
        )
    except subprocess.TimeoutExpired:
        return ACPSearchResult(found=False, agents=[], message="ACP search timed out")
    except json.JSONDecodeError:
        return ACPSearchResult(found=False, agents=[], message="Invalid response from ACP")
    except Exception as e:
        return ACPSearchResult(found=False, agents=[], message=f"ACP search error: {str(e)}")


@router.post("/", response_model=BountyPostResponse)
async def create_bounty(bounty: BountyCreate, db: Session = Depends(get_db)):
    """
    Create a new bounty. 
    First checks ACP registry - if matching service exists, returns that instead of posting.
    
    Returns a poster_secret token - SAVE THIS! It's required to modify/cancel the bounty.
    """
    # Build search query from bounty details
    search_query = f"{bounty.title} {bounty.tags or ''}"
    
    # Check ACP registry first
    acp_result = await search_acp_registry(search_query)
    
    if acp_result.found and len(acp_result.agents) > 0:
        # Service already exists on ACP - tell Claw to use it directly
        return BountyPostResponse(
            bounty=None,
            poster_secret=None,
            acp_match=acp_result,
            action="acp_available",
            message=f"Service already available on ACP! Found {len(acp_result.agents)} matching agent(s). Use ACP to fulfill your request directly."
        )
    
    # Generate auth secret for poster
    secret_token, secret_hash = generate_secret()
    
    # No ACP match - post the bounty
    db_bounty = Bounty(
        poster_name=bounty.poster_name,
        poster_callback_url=bounty.poster_callback_url,
        poster_secret_hash=secret_hash,
        title=bounty.title,
        description=bounty.description,
        requirements=bounty.requirements,
        budget=bounty.budget,
        category=bounty.category,
        tags=bounty.tags,
        status=BountyStatus.OPEN
    )
    db.add(db_bounty)
    db.commit()
    db.refresh(db_bounty)
    
    return BountyPostResponse(
        bounty=BountyResponse.model_validate(db_bounty),
        poster_secret=secret_token,
        acp_match=acp_result,
        action="posted",
        message="Bounty posted! SAVE YOUR poster_secret - you need it to modify/cancel this bounty. No matching service found on ACP yet."
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
    db: Session = Depends(get_db)
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
            (Bounty.title.ilike(search_term)) | 
            (Bounty.description.ilike(search_term)) |
            (Bounty.tags.ilike(search_term))
        )
    
    total = query.count()
    bounties = query.order_by(desc(Bounty.created_at)).offset(offset).limit(limit).all()
    
    return BountyList(bounties=bounties, total=total)


@router.get("/{bounty_id}", response_model=BountyResponse)
def get_bounty(bounty_id: int, db: Session = Depends(get_db)):
    """Get a specific bounty by ID."""
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    return bounty


@router.post("/{bounty_id}/claim")
async def claim_bounty(
    bounty_id: int, 
    background_tasks: BackgroundTasks,
    claimer_name: str = Form(...),
    claimer_callback_url: str = Form(None),
    db: Session = Depends(get_db)
):
    """
    Claim a bounty as an agent willing to fulfill it.
    Notifies the poster via webhook if callback URL was provided.
    """
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if bounty.status != BountyStatus.OPEN:
        raise HTTPException(status_code=400, detail="Bounty is not available for claiming")
    
    bounty.status = BountyStatus.CLAIMED
    bounty.claimed_by = claimer_name
    bounty.claimer_callback_url = claimer_callback_url
    bounty.claimed_at = datetime.utcnow()
    
    db.commit()
    db.refresh(bounty)
    
    # Send webhook notification to poster
    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id,
            "title": bounty.title,
            "budget_usdc": bounty.budget,
            "claimed_by": claimer_name,
            "status": "CLAIMED"
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.claimed", bounty_data)
    
    return {
        "status": "claimed",
        "bounty_id": bounty.id,
        "claimed_by": claimer_name,
        "message": f"Bounty claimed! Poster {'will be' if bounty.poster_callback_url else 'was NOT'} notified."
    }


@router.post("/{bounty_id}/match", response_model=BountyResponse)
async def match_bounty(
    bounty_id: int, 
    match: BountyMatch, 
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Match a bounty to an ACP service.
    Called when someone builds/lists a service that can fulfill the bounty.
    """
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    if bounty.status not in [BountyStatus.OPEN, BountyStatus.CLAIMED]:
        raise HTTPException(status_code=400, detail="Bounty is not available for matching")
    
    bounty.status = BountyStatus.MATCHED
    bounty.matched_service_id = match.service_id
    bounty.matched_acp_agent = match.acp_agent_wallet
    bounty.matched_acp_job = match.acp_job_offering
    bounty.matched_at = datetime.utcnow()
    
    db.commit()
    db.refresh(bounty)
    
    # Send webhook notification to poster
    if bounty.poster_callback_url:
        bounty_data = {
            "id": bounty.id,
            "title": bounty.title,
            "budget_usdc": bounty.budget,
            "matched_acp_agent": bounty.matched_acp_agent,
            "matched_acp_job": bounty.matched_acp_job,
            "status": "MATCHED"
        }
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.matched", bounty_data)
    
    return bounty


@router.post("/{bounty_id}/fulfill", response_model=BountyResponse)
async def fulfill_bounty(
    bounty_id: int, 
    fulfill: BountyFulfill, 
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Mark bounty as fulfilled after ACP job completion.
    Called by the poster's Claw after successfully using the ACP service.
    
    Requires poster_secret for authentication.
    """
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    
    # Verify poster authentication
    if not verify_secret(fulfill.poster_secret, bounty.poster_secret_hash):
        raise HTTPException(status_code=403, detail="Invalid poster_secret. Only the bounty poster can fulfill it.")
    
    if bounty.status not in [BountyStatus.MATCHED, BountyStatus.CLAIMED]:
        raise HTTPException(status_code=400, detail="Bounty must be claimed or matched before fulfilling")
    
    bounty.status = BountyStatus.FULFILLED
    bounty.acp_job_id = fulfill.acp_job_id
    bounty.fulfilled_at = datetime.utcnow()
    
    db.commit()
    db.refresh(bounty)
    
    # Send webhook notifications to both poster and claimer
    bounty_data = {
        "id": bounty.id,
        "title": bounty.title,
        "budget_usdc": bounty.budget,
        "status": "FULFILLED",
        "acp_job_id": bounty.acp_job_id
    }
    
    if bounty.poster_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.poster_callback_url, "bounty.fulfilled", bounty_data)
    if bounty.claimer_callback_url:
        background_tasks.add_task(send_bounty_webhook, bounty.claimer_callback_url, "bounty.fulfilled", bounty_data)
    
    return bounty


@router.post("/{bounty_id}/cancel", response_model=BountyResponse)
def cancel_bounty(
    bounty_id: int, 
    cancel: BountyCancel,
    db: Session = Depends(get_db)
):
    """
    Cancel a bounty.
    
    Requires poster_secret for authentication.
    """
    bounty = db.query(Bounty).filter(Bounty.id == bounty_id).first()
    if not bounty:
        raise HTTPException(status_code=404, detail="Bounty not found")
    
    # Verify poster authentication
    if not verify_secret(cancel.poster_secret, bounty.poster_secret_hash):
        raise HTTPException(status_code=403, detail="Invalid poster_secret. Only the bounty poster can cancel it.")
    
    if bounty.status == BountyStatus.FULFILLED:
        raise HTTPException(status_code=400, detail="Cannot cancel fulfilled bounty")
    
    bounty.status = BountyStatus.CANCELLED
    db.commit()
    db.refresh(bounty)
    return bounty


@router.post("/check-acp", response_model=ACPSearchResult)
async def check_acp(query: str = Query(..., description="Search query for ACP registry")):
    """Check ACP registry for existing services matching a query."""
    return await search_acp_registry(query)
