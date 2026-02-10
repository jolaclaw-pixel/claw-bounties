"""Shared business logic for service (listing) operations."""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Bounty, BountyStatus, Service, generate_secret
from app.utils import sanitize_text

logger = logging.getLogger(__name__)


def create_service(
    db: Session,
    *,
    agent_name: str,
    name: str,
    description: str,
    price: float,
    category: str = "digital",
    location: Optional[str] = None,
    shipping_available: bool = False,
    tags: Optional[str] = None,
    acp_agent_wallet: Optional[str] = None,
    acp_job_offering: Optional[str] = None,
) -> tuple[Service, str]:
    """Create a service listing in the DB.

    Args:
        db: Database session.
        agent_name: Name of the agent listing the service.
        name: Service name.
        description: Service description.
        price: Price in USDC.
        category: Category string.
        location: Optional location string.
        shipping_available: Whether shipping is available.
        tags: Optional comma-separated tags.
        acp_agent_wallet: Optional ACP wallet address.
        acp_job_offering: Optional ACP job offering name.

    Returns:
        Tuple of (service, plaintext_secret).
    """
    secret_token, secret_hash = generate_secret()

    service = Service(
        agent_name=sanitize_text(agent_name),
        agent_secret_hash=secret_hash,
        name=sanitize_text(name),
        description=sanitize_text(description),
        price=price,
        category=category,
        location=sanitize_text(location) if location else None,
        shipping_available=shipping_available,
        tags=sanitize_text(tags) if tags else None,
        acp_agent_wallet=acp_agent_wallet,
        acp_job_offering=acp_job_offering,
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return service, secret_token


def auto_match_bounties(db: Session, service: Service) -> None:
    """Find and match open bounties that this service can fulfill.

    Args:
        db: Database session.
        service: The newly created service to match against open bounties.
    """
    open_bounties = (
        db.query(Bounty)
        .filter(Bounty.status == BountyStatus.OPEN, Bounty.category == service.category)
        .all()
    )

    service_tags = set(t.strip().lower() for t in (service.tags or "").split(",") if t.strip())
    service_words = set(service.name.lower().split() + service.description.lower().split()[:20])

    for bounty in open_bounties:
        bounty_tags = set(t.strip().lower() for t in (bounty.tags or "").split(",") if t.strip())
        bounty_words = set(bounty.title.lower().split() + bounty.description.lower().split()[:20])

        tag_match = len(service_tags & bounty_tags) > 0
        word_match = len(service_words & bounty_words) >= 2

        if tag_match or word_match:
            bounty.status = BountyStatus.MATCHED
            bounty.matched_service_id = service.id
            bounty.matched_acp_agent = service.acp_agent_wallet
            bounty.matched_acp_job = service.acp_job_offering
            bounty.matched_at = datetime.now(timezone.utc)

    db.commit()
