from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc
from typing import Optional
from datetime import datetime

from app.database import get_db
from app.models import Service, Bounty, BountyStatus
from app.schemas import ServiceCreate, ServiceResponse, ServiceList

router = APIRouter(prefix="/api/services", tags=["services"])


@router.post("/", response_model=ServiceResponse)
def create_service(service: ServiceCreate, db: Session = Depends(get_db)):
    """
    List a new service or resource.
    After listing, checks for matching open bounties.
    """
    db_service = Service(
        agent_name=service.agent_name,
        name=service.name,
        description=service.description,
        price=service.price,
        category=service.category,
        location=service.location,
        shipping_available=service.shipping_available,
        tags=service.tags,
        acp_agent_wallet=service.acp_agent_wallet,
        acp_job_offering=service.acp_job_offering
    )
    db.add(db_service)
    db.commit()
    db.refresh(db_service)
    
    # Auto-match open bounties if ACP details provided
    if service.acp_agent_wallet and service.acp_job_offering:
        _auto_match_bounties(db, db_service)
    
    return db_service


def _auto_match_bounties(db: Session, service: Service):
    """Find and match open bounties that this service can fulfill."""
    # Search for matching bounties by tags/category
    open_bounties = db.query(Bounty).filter(
        Bounty.status == BountyStatus.OPEN,
        Bounty.category == service.category
    ).all()
    
    service_tags = set(t.strip().lower() for t in (service.tags or "").split(",") if t.strip())
    service_words = set(service.name.lower().split() + service.description.lower().split()[:20])
    
    for bounty in open_bounties:
        bounty_tags = set(t.strip().lower() for t in (bounty.tags or "").split(",") if t.strip())
        bounty_words = set(bounty.title.lower().split() + bounty.description.lower().split()[:20])
        
        # Check for tag overlap or keyword overlap
        tag_match = len(service_tags & bounty_tags) > 0
        word_match = len(service_words & bounty_words) >= 2
        
        if tag_match or word_match:
            # Match this bounty
            bounty.status = BountyStatus.MATCHED
            bounty.matched_service_id = service.id
            bounty.matched_acp_agent = service.acp_agent_wallet
            bounty.matched_acp_job = service.acp_job_offering
            bounty.matched_at = datetime.utcnow()
            
            # TODO: Notify bounty poster's Claw via callback_url
    
    db.commit()


@router.get("/", response_model=ServiceList)
def list_services(
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    search: Optional[str] = None,
    location: Optional[str] = None,
    shipping_available: Optional[bool] = None,
    acp_only: Optional[bool] = None,
    limit: int = Query(default=50, le=100),
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """List services with optional filters."""
    query = db.query(Service).filter(Service.is_active == True)
    
    if category:
        query = query.filter(Service.category == category)
    if min_price:
        query = query.filter(Service.price >= min_price)
    if max_price:
        query = query.filter(Service.price <= max_price)
    if location:
        query = query.filter(Service.location.ilike(f"%{location}%"))
    if shipping_available is not None:
        query = query.filter(Service.shipping_available == shipping_available)
    if acp_only:
        query = query.filter(Service.acp_agent_wallet.isnot(None))
    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (Service.name.ilike(search_term)) | 
            (Service.description.ilike(search_term)) |
            (Service.tags.ilike(search_term))
        )
    
    total = query.count()
    services = query.order_by(desc(Service.created_at)).offset(offset).limit(limit).all()
    
    return ServiceList(services=services, total=total)


@router.get("/{service_id}", response_model=ServiceResponse)
def get_service(service_id: int, db: Session = Depends(get_db)):
    """Get a specific service by ID."""
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    return service


@router.put("/{service_id}", response_model=ServiceResponse)
def update_service(service_id: int, service_update: ServiceCreate, db: Session = Depends(get_db)):
    """Update a service listing."""
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    for key, value in service_update.model_dump().items():
        setattr(service, key, value)
    
    db.commit()
    db.refresh(service)
    return service


@router.delete("/{service_id}")
def deactivate_service(service_id: int, db: Session = Depends(get_db)):
    """Deactivate a service listing."""
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    service.is_active = False
    db.commit()
    return {"message": "Service deactivated"}
