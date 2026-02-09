"""Pydantic schemas for request/response validation."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import (
    MAX_BUDGET,
    MAX_DESCRIPTION_LENGTH,
    MAX_NAME_LENGTH,
    MAX_PRICE,
    MAX_REQUIREMENTS_LENGTH,
    MAX_TAG_LENGTH,
    MAX_TITLE_LENGTH,
    MAX_URL_LENGTH,
    MIN_DESCRIPTION_LENGTH,
    MIN_TITLE_LENGTH,
    VALID_CATEGORIES,
)
from app.models import BountyStatus, ServiceCategory  # noqa: F401 — single source of truth


# Service schemas


class ServiceCreate(BaseModel):
    """Schema for creating a new service listing."""

    agent_name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, examples=["MyAgent"])
    name: str = Field(..., min_length=1, max_length=MAX_TITLE_LENGTH, examples=["Logo Design Service"])
    description: str = Field(..., min_length=1, max_length=MAX_DESCRIPTION_LENGTH, examples=["Professional logo design for your project"])
    price: float = Field(..., gt=0, le=MAX_PRICE, description="Price in USDC", examples=[50.0])
    category: ServiceCategory = Field(default=ServiceCategory.DIGITAL, examples=["digital"])
    location: Optional[str] = Field(None, max_length=200, examples=["San Francisco, CA"])
    shipping_available: bool = Field(default=False, examples=[False])
    tags: Optional[str] = Field(None, max_length=MAX_TAG_LENGTH, examples=["design,logo,branding"])
    acp_agent_wallet: Optional[str] = Field(None, max_length=42, examples=["0x1234567890abcdef1234567890abcdef12345678"])
    acp_job_offering: Optional[str] = Field(None, max_length=200, examples=["logo-design"])


class ServiceResponse(BaseModel):
    """Schema for service listing responses."""

    id: int = Field(..., examples=[1])
    agent_name: str = Field(..., examples=["MyAgent"])
    name: str = Field(..., examples=["Logo Design Service"])
    description: str = Field(..., examples=["Professional logo design"])
    price: float = Field(..., examples=[50.0])
    category: str = Field(..., examples=["digital"])
    location: Optional[str] = Field(None, examples=["San Francisco, CA"])
    shipping_available: bool = Field(..., examples=[False])
    tags: Optional[str] = Field(None, examples=["design,logo"])
    acp_agent_wallet: Optional[str] = None
    acp_job_offering: Optional[str] = None
    created_at: datetime
    is_active: bool = Field(..., examples=[True])

    class Config:
        from_attributes = True


# Bounty schemas


class BountyCreate(BaseModel):
    """Schema for creating a new bounty."""

    poster_name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, examples=["MyAgent"])
    poster_callback_url: Optional[str] = Field(None, max_length=MAX_URL_LENGTH, examples=["https://example.com/webhook"])
    title: str = Field(..., min_length=MIN_TITLE_LENGTH, max_length=MAX_TITLE_LENGTH, examples=["Need a logo designed"])
    description: str = Field(..., min_length=MIN_DESCRIPTION_LENGTH, max_length=MAX_DESCRIPTION_LENGTH, examples=["Design a professional logo for my project"])
    requirements: Optional[str] = Field(None, max_length=MAX_REQUIREMENTS_LENGTH, examples=["Must be SVG format"])
    budget: float = Field(..., gt=0, le=MAX_BUDGET, description="Budget in USDC", examples=[100.0])
    category: ServiceCategory = Field(default=ServiceCategory.DIGITAL, examples=["digital"])
    tags: Optional[str] = Field(None, max_length=MAX_TAG_LENGTH, examples=["design,logo"])

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v: str) -> str:
        """Ensure category is a valid option."""
        if isinstance(v, str) and v.lower() not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{v}'. Must be one of: {', '.join(VALID_CATEGORIES)}")
        return v


class BountyClaim(BaseModel):
    """Used when claiming a bounty."""

    claimer_name: str = Field(..., min_length=1, max_length=MAX_NAME_LENGTH, examples=["ClaimerAgent"])
    claimer_callback_url: Optional[str] = Field(None, examples=["https://example.com/webhook"])


class BountyClaimResponse(BaseModel):
    """Response when bounty is claimed — includes one-time secret."""

    bounty_id: int = Field(..., examples=[1])
    claimed_by: str = Field(..., examples=["ClaimerAgent"])
    claimer_secret: str = Field(..., description="Save this! Required to unclaim or provide deliverables. Shown only once.", examples=["abc123secret"])
    message: str = Field(..., examples=["Bounty claimed! SAVE YOUR claimer_secret."])


class BountyMatch(BaseModel):
    """Used when matching a bounty to an ACP service — requires poster auth."""

    poster_secret: str = Field(..., description="Secret token returned when bounty was created")
    service_id: Optional[int] = None
    acp_agent_wallet: str = Field(..., examples=["0x1234567890abcdef1234567890abcdef12345678"])
    acp_job_offering: str = Field(..., examples=["logo-design"])


class BountyUnclaim(BaseModel):
    """Used when unclaiming a bounty."""

    claimer_secret: str = Field(..., description="Secret token returned when bounty was claimed")


class BountyFulfill(BaseModel):
    """Used when bounty is fulfilled via ACP."""

    acp_job_id: str = Field(..., examples=["job-123"])
    poster_secret: str = Field(..., description="Secret token returned when bounty was created")


class BountyCancel(BaseModel):
    """Used when cancelling a bounty."""

    poster_secret: str = Field(..., description="Secret token returned when bounty was created")


class BountyResponse(BaseModel):
    """Schema for bounty responses."""

    id: int = Field(..., examples=[1])
    poster_name: str = Field(..., examples=["MyAgent"])
    poster_callback_url: Optional[str] = None
    title: str = Field(..., examples=["Need a logo designed"])
    description: str = Field(..., examples=["Design a professional logo"])
    requirements: Optional[str] = None
    budget: float = Field(..., examples=[100.0])
    category: str = Field(..., examples=["digital"])
    tags: Optional[str] = None
    status: str = Field(..., examples=["open"])
    matched_service_id: Optional[int] = None
    matched_acp_agent: Optional[str] = None
    matched_acp_job: Optional[str] = None
    matched_at: Optional[datetime] = None
    acp_job_id: Optional[str] = None
    fulfilled_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ACP Agent info from registry


class ACPAgent(BaseModel):
    """ACP agent info from the registry cache."""

    wallet_address: str = Field(..., examples=["0xabc123"])
    name: str = Field(..., examples=["TraderBot"])
    description: str = Field(..., examples=["Automated trading agent"])
    job_offerings: List[str] = Field(default_factory=list, examples=[["trade-execution"]])


class ACPSearchResult(BaseModel):
    """Result of an ACP agent search."""

    found: bool = Field(..., examples=[True])
    agents: List[ACPAgent] = []
    message: str = Field(..., examples=["Found 3 matching service(s) on ACP"])


# List responses


class ServiceList(BaseModel):
    """Response for service listing endpoints."""

    services: List[ServiceResponse]
    total: int = Field(..., examples=[42])


class BountyList(BaseModel):
    """Response for bounty listing endpoints."""

    bounties: List[BountyResponse]
    total: int = Field(..., examples=[10])


# Bounty creation response — includes secret (ONE TIME ONLY)


class BountyCreatedResponse(BaseModel):
    """Response after creating a bounty — includes the one-time secret."""

    bounty: BountyResponse
    poster_secret: str = Field(..., description="Save this! Required to modify/cancel bounty. Shown only once.")


# Bounty post response — includes ACP check


class BountyPostResponse(BaseModel):
    """Response after posting a bounty — includes ACP match check."""

    bounty: Optional[BountyResponse] = None
    poster_secret: Optional[str] = Field(None, description="Save this! Required to modify/cancel bounty. Shown only once.")
    acp_match: Optional[ACPSearchResult] = None
    action: str = Field(..., examples=["posted"])  # "posted" | "acp_available"
    message: str = Field(..., examples=["Bounty posted!"])


# Service creation response — includes secret


class ServiceCreatedResponse(BaseModel):
    """Response after creating a service — includes the one-time secret."""

    service: ServiceResponse
    agent_secret: str = Field(..., description="Save this! Required to modify/delete service. Shown only once.")


class ServiceUpdate(BaseModel):
    """Used when updating a service."""

    agent_secret: str = Field(..., description="Secret token returned when service was created")
    name: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = None
    category: Optional[ServiceCategory] = None
    location: Optional[str] = None
    shipping_available: Optional[bool] = None
    tags: Optional[str] = None
    acp_agent_wallet: Optional[str] = None
    acp_job_offering: Optional[str] = None


class ServiceDelete(BaseModel):
    """Used when deleting a service."""

    agent_secret: str = Field(..., description="Secret token returned when service was created")


# Pagination metadata


class PaginationMeta(BaseModel):
    """Pagination metadata for list responses."""

    total: int = Field(..., examples=[42])
    page: int = Field(..., examples=[1])
    per_page: int = Field(..., examples=[50])


class EnvelopedBountyList(BaseModel):
    """Enveloped response for bounty list endpoints."""

    data: List[BountyResponse]
    meta: PaginationMeta
    # Keep backward compat
    bounties: List[BountyResponse]
    total: int


class EnvelopedServiceList(BaseModel):
    """Enveloped response for service list endpoints."""

    data: List[ServiceResponse]
    meta: PaginationMeta
    # Keep backward compat
    services: List[ServiceResponse]
    total: int
