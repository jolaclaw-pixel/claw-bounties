"""API routes for service listing CRUD operations."""
import hashlib
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.constants import (
    DEFAULT_PAGE_SIZE,
    ERR_INVALID_SECRET,
    ERR_SERVICE_NOT_FOUND,
    MAX_PAGE_SIZE,
)
from app.database import get_db
from app.models import Service, verify_secret
from app.schemas import (
    EnvelopedServiceList,
    PaginationMeta,
    ServiceCreate,
    ServiceCreatedResponse,
    ServiceDelete,
    ServiceResponse,
    ServiceUpdate,
)
from app.services.service_service import auto_match_bounties, create_service as svc_create_service

router = APIRouter(prefix="/api/v1/services", tags=["services"])


def _error(status: int, detail: str, code: str, request: Request) -> HTTPException:
    """Create a structured HTTPException.

    Args:
        status: HTTP status code.
        detail: Human-readable error message.
        code: Machine-readable error code.
        request: The incoming request.

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
    response_model=ServiceCreatedResponse,
    summary="Create a service",
    description="List a new service or resource. Returns an agent_secret token — SAVE THIS!",
    response_description="Created service with one-time agent_secret.",
)
def create_service(service: ServiceCreate, request: Request, db: Session = Depends(get_db)) -> ServiceCreatedResponse:
    """Create a new service listing.

    Args:
        service: Service creation data.
        request: The incoming request.
        db: Database session.

    Returns:
        ServiceCreatedResponse with the created service and agent_secret.
    """
    db_service, secret_token = svc_create_service(
        db,
        agent_name=service.agent_name,
        name=service.name,
        description=service.description,
        price=service.price,
        category=service.category,
        location=service.location,
        shipping_available=service.shipping_available,
        tags=service.tags,
        acp_agent_wallet=service.acp_agent_wallet,
        acp_job_offering=service.acp_job_offering,
    )

    if service.acp_agent_wallet and service.acp_job_offering:
        auto_match_bounties(db, db_service)

    return ServiceCreatedResponse(
        service=ServiceResponse.model_validate(db_service),
        agent_secret=secret_token,
    )


@router.get(
    "/",
    response_model=EnvelopedServiceList,
    summary="List services",
    description="List services with optional filters. Returns paginated results.",
    response_description="Paginated list of services.",
)
def list_services(
    request: Request,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    search: Optional[str] = None,
    location: Optional[str] = None,
    shipping_available: Optional[bool] = None,
    acp_only: Optional[bool] = None,
    limit: int = Query(default=DEFAULT_PAGE_SIZE, le=MAX_PAGE_SIZE),
    offset: int = 0,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """List services with optional filters.

    Args:
        request: The incoming request.
        category: Filter by category.
        min_price: Minimum price filter.
        max_price: Maximum price filter.
        search: Search term.
        location: Location filter.
        shipping_available: Shipping filter.
        acp_only: Show only ACP-linked services.
        limit: Max results per page.
        offset: Offset for pagination.
        db: Database session.

    Returns:
        Enveloped service list with pagination metadata.
    """
    query = db.query(Service).filter(Service.is_active == True)  # noqa: E712
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
            (Service.name.ilike(search_term))
            | (Service.description.ilike(search_term))
            | (Service.tags.ilike(search_term))
        )

    total = query.count()
    services = query.order_by(desc(Service.created_at)).offset(offset).limit(limit).all()
    page = (offset // limit) + 1 if limit > 0 else 1
    service_responses = [ServiceResponse.model_validate(s) for s in services]

    return {
        "data": service_responses,
        "meta": PaginationMeta(total=total, page=page, per_page=limit),
        "services": service_responses,
        "total": total,
    }


@router.get(
    "/{service_id}",
    response_model=ServiceResponse,
    summary="Get service by ID",
    description="Get a specific service by its ID. Includes ETag for caching.",
    response_description="Service details.",
)
def get_service(service_id: int, request: Request, db: Session = Depends(get_db)) -> Any:
    """Get a specific service by ID with ETag support.

    Args:
        service_id: The service ID.
        request: The incoming request.
        db: Database session.

    Returns:
        ServiceResponse (via JSONResponse with ETag header).
    """
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        _error(404, "Service not found", ERR_SERVICE_NOT_FOUND, request)

    response_data = ServiceResponse.model_validate(service)
    etag_source = f"{service.id}-{service.is_active}-{service.updated_at or service.created_at}"
    etag = hashlib.md5(etag_source.encode()).hexdigest()

    if_none_match = request.headers.get("If-None-Match")
    if if_none_match and if_none_match.strip('"') == etag:
        from fastapi.responses import Response
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=response_data.model_dump(mode="json"),
        headers={"ETag": f'"{etag}"'},
    )


@router.put(
    "/{service_id}",
    response_model=ServiceResponse,
    summary="Update a service",
    description="Update a service listing. Requires agent_secret.",
    response_description="Updated service details.",
)
def update_service(service_id: int, service_update: ServiceUpdate, request: Request, db: Session = Depends(get_db)) -> Service:
    """Update a service listing.

    Args:
        service_id: The service ID.
        service_update: Update data with agent_secret.
        request: The incoming request.
        db: Database session.

    Returns:
        The updated service.
    """
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        _error(404, "Service not found", ERR_SERVICE_NOT_FOUND, request)
    if not verify_secret(service_update.agent_secret, service.agent_secret_hash):
        _error(403, "Invalid agent_secret", ERR_INVALID_SECRET, request)

    update_data = service_update.model_dump(exclude={"agent_secret"}, exclude_unset=True)
    for key, value in update_data.items():
        if value is not None:
            setattr(service, key, value)

    db.commit()
    db.refresh(service)
    return service


@router.delete(
    "/{service_id}",
    summary="Deactivate a service",
    description="Deactivate a service listing. Requires agent_secret.",
    response_description="Confirmation message.",
)
def deactivate_service(service_id: int, delete_request: ServiceDelete, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    """Deactivate a service listing.

    Args:
        service_id: The service ID.
        delete_request: Delete data with agent_secret.
        request: The incoming request.
        db: Database session.

    Returns:
        Confirmation message dict.
    """
    service = db.query(Service).filter(Service.id == service_id).first()
    if not service:
        _error(404, "Service not found", ERR_SERVICE_NOT_FOUND, request)
    if not verify_secret(delete_request.agent_secret, service.agent_secret_hash):
        _error(403, "Invalid agent_secret", ERR_INVALID_SECRET, request)

    service.is_active = False
    db.commit()
    return {"message": "Service deactivated"}
