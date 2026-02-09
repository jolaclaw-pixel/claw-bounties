from sqlalchemy import Column, Integer, String, Text, Float, DateTime, Boolean
from sqlalchemy.sql import func
from app.database import Base
import enum
import secrets
import hashlib


def generate_secret() -> tuple[str, str]:
    """Generate a secret token and its hash. Returns (plaintext, hash)."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return token, token_hash


def verify_secret(provided: str, stored_hash: str) -> bool:
    """Verify a provided secret against stored hash."""
    if not provided or not stored_hash:
        return False
    provided_hash = hashlib.sha256(provided.encode()).hexdigest()
    return secrets.compare_digest(provided_hash, stored_hash)


class ServiceCategory(str, enum.Enum):
    DIGITAL = "digital"
    PHYSICAL = "physical"


class BountyStatus(str, enum.Enum):
    OPEN = "open"
    CLAIMED = "claimed"
    MATCHED = "matched"
    FULFILLED = "fulfilled"
    CANCELLED = "cancelled"


class Service(Base):
    """Services listed on the bounty platform (local registry)"""
    __tablename__ = "services"

    id = Column(Integer, primary_key=True, index=True)
    agent_name = Column(String(100), nullable=False)
    agent_secret_hash = Column(String(64), nullable=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    price = Column(Float, nullable=False)
    category = Column(String(20), default=ServiceCategory.DIGITAL, index=True)

    location = Column(String(200), nullable=True)
    shipping_available = Column(Boolean, default=False)

    tags = Column(String(500), nullable=True)

    acp_agent_wallet = Column(String(42), nullable=True)
    acp_job_offering = Column(String(200), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    is_active = Column(Boolean, default=True, index=True)


class Bounty(Base):
    """Bounties posted by Claws looking for services"""
    __tablename__ = "bounties"

    id = Column(Integer, primary_key=True, index=True)
    poster_name = Column(String(100), nullable=False, index=True)
    poster_callback_url = Column(String(500), nullable=True)
    poster_secret_hash = Column(String(64), nullable=True)

    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=False)
    requirements = Column(Text, nullable=True)

    budget = Column(Float, nullable=False)

    category = Column(String(20), default=ServiceCategory.DIGITAL, index=True)
    tags = Column(String(500), nullable=True)

    status = Column(String(20), default=BountyStatus.OPEN, index=True)

    claimed_by = Column(String(100), nullable=True)
    claimer_callback_url = Column(String(500), nullable=True)
    claimer_secret_hash = Column(String(64), nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)

    matched_service_id = Column(Integer, nullable=True)
    matched_acp_agent = Column(String(42), nullable=True)
    matched_acp_job = Column(String(200), nullable=True)
    matched_at = Column(DateTime(timezone=True), nullable=True)

    acp_job_id = Column(String(100), nullable=True)
    fulfilled_at = Column(DateTime(timezone=True), nullable=True)

    expires_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
