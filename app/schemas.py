from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime

from app.models import ServiceCategory, BountyStatus  # noqa: F401 — single source of truth

VALID_CATEGORIES = {"digital", "physical"}


# Service schemas
class ServiceCreate(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)
    description: str = Field(..., min_length=1, max_length=5000)
    price: float = Field(..., gt=0, le=1_000_000, description="Price in USDC")
    category: ServiceCategory = ServiceCategory.DIGITAL
    location: Optional[str] = Field(None, max_length=200)
    shipping_available: bool = False
    tags: Optional[str] = Field(None, max_length=500)
    acp_agent_wallet: Optional[str] = Field(None, max_length=42)
    acp_job_offering: Optional[str] = Field(None, max_length=200)


class ServiceResponse(BaseModel):
    id: int
    agent_name: str
    name: str
    description: str
    price: float
    category: str
    location: Optional[str]
    shipping_available: bool
    tags: Optional[str]
    acp_agent_wallet: Optional[str]
    acp_job_offering: Optional[str]
    created_at: datetime
    is_active: bool

    class Config:
        from_attributes = True


# Bounty schemas
class BountyCreate(BaseModel):
    poster_name: str = Field(..., min_length=1, max_length=100)
    poster_callback_url: Optional[str] = Field(None, max_length=500)
    title: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10, max_length=5000)
    requirements: Optional[str] = Field(None, max_length=2000)
    budget: float = Field(..., gt=0, le=1_000_000, description="Budget in USDC")
    category: ServiceCategory = ServiceCategory.DIGITAL
    tags: Optional[str] = Field(None, max_length=500)

    @field_validator("category", mode="before")
    @classmethod
    def validate_category(cls, v: str) -> str:
        """Ensure category is a valid option."""
        if isinstance(v, str) and v.lower() not in VALID_CATEGORIES:
            raise ValueError(f"Invalid category '{v}'. Must be one of: {', '.join(VALID_CATEGORIES)}")
        return v


class BountyClaim(BaseModel):
    """Used when claiming a bounty"""
    claimer_name: str = Field(..., min_length=1, max_length=100)
    claimer_callback_url: Optional[str] = None


class BountyClaimResponse(BaseModel):
    """Response when bounty is claimed - includes one-time secret"""
    bounty_id: int
    claimed_by: str
    claimer_secret: str = Field(..., description="Save this! Required to unclaim or provide deliverables. Shown only once.")
    message: str


class BountyMatch(BaseModel):
    """Used when matching a bounty to an ACP service - requires poster auth"""
    poster_secret: str = Field(..., description="Secret token returned when bounty was created")
    service_id: Optional[int] = None
    acp_agent_wallet: str
    acp_job_offering: str


class BountyUnclaim(BaseModel):
    """Used when unclaiming a bounty"""
    claimer_secret: str = Field(..., description="Secret token returned when bounty was claimed")


class BountyFulfill(BaseModel):
    """Used when bounty is fulfilled via ACP"""
    acp_job_id: str
    poster_secret: str = Field(..., description="Secret token returned when bounty was created")


class BountyCancel(BaseModel):
    """Used when cancelling a bounty"""
    poster_secret: str = Field(..., description="Secret token returned when bounty was created")


class BountyResponse(BaseModel):
    id: int
    poster_name: str
    poster_callback_url: Optional[str]
    title: str
    description: str
    requirements: Optional[str]
    budget: float
    category: str
    tags: Optional[str]
    status: str
    matched_service_id: Optional[int]
    matched_acp_agent: Optional[str]
    matched_acp_job: Optional[str]
    matched_at: Optional[datetime]
    acp_job_id: Optional[str]
    fulfilled_at: Optional[datetime]
    created_at: datetime

    class Config:
        from_attributes = True


# ACP Agent info from registry
class ACPAgent(BaseModel):
    wallet_address: str
    name: str
    description: str
    job_offerings: List[str]


class ACPSearchResult(BaseModel):
    found: bool
    agents: List[ACPAgent] = []
    message: str


# List responses
class ServiceList(BaseModel):
    services: List[ServiceResponse]
    total: int


class BountyList(BaseModel):
    bounties: List[BountyResponse]
    total: int


# Bounty creation response - includes secret (ONE TIME ONLY)
class BountyCreatedResponse(BaseModel):
    bounty: BountyResponse
    poster_secret: str = Field(..., description="Save this! Required to modify/cancel bounty. Shown only once.")


# Bounty post response - includes ACP check
class BountyPostResponse(BaseModel):
    bounty: Optional[BountyResponse] = None
    poster_secret: Optional[str] = Field(None, description="Save this! Required to modify/cancel bounty. Shown only once.")
    acp_match: Optional[ACPSearchResult] = None
    action: str  # "posted" | "acp_available"
    message: str


# Service creation response - includes secret
class ServiceCreatedResponse(BaseModel):
    service: ServiceResponse
    agent_secret: str = Field(..., description="Save this! Required to modify/delete service. Shown only once.")


class ServiceUpdate(BaseModel):
    """Used when updating a service"""
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
    """Used when deleting a service"""
    agent_secret: str = Field(..., description="Secret token returned when service was created")
