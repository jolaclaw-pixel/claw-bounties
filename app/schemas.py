from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from enum import Enum


class ServiceCategory(str, Enum):
    digital = "digital"
    physical = "physical"


class BountyStatus(str, Enum):
    open = "open"
    matched = "matched"
    fulfilled = "fulfilled"
    cancelled = "cancelled"


# Service schemas
class ServiceCreate(BaseModel):
    agent_name: str = Field(..., min_length=1, max_length=100)
    name: str = Field(..., min_length=1, max_length=200)
    description: str
    price: float = Field(..., gt=0, description="Price in USDC")
    category: ServiceCategory = ServiceCategory.digital
    location: Optional[str] = None
    shipping_available: bool = False
    tags: Optional[str] = None
    acp_agent_wallet: Optional[str] = None
    acp_job_offering: Optional[str] = None


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
    poster_callback_url: Optional[str] = None
    title: str = Field(..., min_length=1, max_length=200)
    description: str
    requirements: Optional[str] = None
    budget: float = Field(..., gt=0, description="Budget in USDC")
    category: ServiceCategory = ServiceCategory.digital
    tags: Optional[str] = None


class BountyMatch(BaseModel):
    """Used when matching a bounty to an ACP service"""
    service_id: Optional[int] = None
    acp_agent_wallet: str
    acp_job_offering: str


class BountyFulfill(BaseModel):
    """Used when bounty is fulfilled via ACP"""
    acp_job_id: str


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


# Bounty post response - includes ACP check
class BountyPostResponse(BaseModel):
    bounty: Optional[BountyResponse] = None
    acp_match: Optional[ACPSearchResult] = None
    action: str  # "posted" | "acp_available"
    message: str
