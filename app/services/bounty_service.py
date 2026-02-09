"""Shared business logic for bounty operations."""
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.constants import (
    BOUNTY_EXPIRY_DAYS,
    WEBHOOK_MAX_RETRIES,
    WEBHOOK_RETRY_BASE_DELAY,
    WEBHOOK_TIMEOUT_SECONDS,
)
from app.models import Bounty, BountyStatus, generate_secret
from app.schemas import ACPAgent, ACPSearchResult
from app.utils import sanitize_text, validate_callback_url

logger = logging.getLogger(__name__)

# --------------- Webhook helpers ---------------


def _sign_payload(payload: dict[str, Any]) -> str:
    """Compute HMAC-SHA256 signature for a webhook payload.

    Args:
        payload: The JSON-serializable payload dict.

    Returns:
        Hex-encoded HMAC-SHA256 signature, or empty string if no secret configured.
    """
    secret = os.getenv("WEBHOOK_HMAC_SECRET", "")
    if not secret:
        return ""
    body = json.dumps(payload, sort_keys=True, default=str)
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


async def send_bounty_webhook(
    callback_url: str,
    event: str,
    bounty_data: dict[str, Any],
) -> None:
    """Send webhook notification for bounty events with retry and HMAC signature.

    Retries up to WEBHOOK_MAX_RETRIES times with exponential backoff.
    Signs payloads with HMAC-SHA256 if WEBHOOK_HMAC_SECRET is configured.

    Args:
        callback_url: The URL to POST the webhook to.
        event: Event type string (e.g., 'bounty.claimed').
        bounty_data: Bounty data dict to include in the payload.
    """
    if not callback_url:
        return
    if not validate_callback_url(callback_url):
        logger.warning(f"Blocked webhook to invalid/private URL: {callback_url}")
        return

    import asyncio

    payload = {
        "event": event,
        "bounty": bounty_data,
        "timestamp": datetime.utcnow().isoformat(),
    }

    headers: dict[str, str] = {"Content-Type": "application/json"}
    signature = _sign_payload(payload)
    if signature:
        headers["X-ClawBounty-Signature"] = f"sha256={signature}"

    for attempt in range(WEBHOOK_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as client:
                response = await client.post(callback_url, json=payload, headers=headers)
                logger.info(f"Webhook sent ({event}) to {callback_url}: {response.status_code}")
                return
        except Exception as e:
            delay = WEBHOOK_RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                f"Webhook attempt {attempt + 1}/{WEBHOOK_MAX_RETRIES} failed for {callback_url}: {e}. "
                f"{'Retrying in ' + str(delay) + 's' if attempt < WEBHOOK_MAX_RETRIES - 1 else 'Giving up (dead letter).'}"
            )
            if attempt < WEBHOOK_MAX_RETRIES - 1:
                await asyncio.sleep(delay)

    logger.error(f"Webhook DEAD LETTER: event={event} url={callback_url} payload={json.dumps(bounty_data, default=str)}")


# --------------- ACP search ---------------


async def search_acp_registry(query: str) -> ACPSearchResult:
    """Search ACP registry using the in-memory cache.

    Args:
        query: Search query string.

    Returns:
        ACPSearchResult with matching agents.
    """
    try:
        from app.acp_registry import search_agents

        results = search_agents(query)
        if not results:
            return ACPSearchResult(found=False, agents=[], message="No matching services found on ACP")

        agents = [
            ACPAgent(
                wallet_address=a.get("wallet_address", ""),
                name=a.get("name", "Unknown"),
                description=a.get("description", ""),
                job_offerings=[j.get("name", "") for j in a.get("job_offerings", [])],
            )
            for a in results
        ]
        return ACPSearchResult(
            found=True, agents=agents, message=f"Found {len(agents)} matching service(s) on ACP"
        )
    except Exception as e:
        return ACPSearchResult(found=False, agents=[], message=f"ACP search error: {str(e)}")


# --------------- Bounty CRUD ---------------


def create_bounty(
    db: Session,
    *,
    poster_name: str,
    title: str,
    description: str,
    budget: float,
    category: str = "digital",
    requirements: Optional[str] = None,
    tags: Optional[str] = None,
    poster_callback_url: Optional[str] = None,
    set_expiry: bool = True,
) -> tuple[Bounty, str]:
    """Create a bounty in the DB.

    Args:
        db: Database session.
        poster_name: Name of the bounty poster.
        title: Bounty title.
        description: Bounty description.
        budget: Budget in USDC.
        category: Category string.
        requirements: Optional requirements text.
        tags: Optional comma-separated tags.
        poster_callback_url: Optional webhook URL.
        set_expiry: Whether to set an expiry date.

    Returns:
        Tuple of (bounty, plaintext_secret).
    """
    secret_token, secret_hash = generate_secret()

    bounty = Bounty(
        poster_name=sanitize_text(poster_name),
        poster_callback_url=poster_callback_url,
        poster_secret_hash=secret_hash,
        title=sanitize_text(title),
        description=sanitize_text(description),
        requirements=sanitize_text(requirements) if requirements else None,
        budget=budget,
        category=category,
        tags=sanitize_text(tags) if tags else None,
        status=BountyStatus.OPEN,
        expires_at=datetime.utcnow() + timedelta(days=BOUNTY_EXPIRY_DAYS) if set_expiry else None,
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)
    return bounty, secret_token


def get_bounty_by_id(db: Session, bounty_id: int) -> Optional[Bounty]:
    """Get a bounty by ID or return None.

    Args:
        db: Database session.
        bounty_id: The bounty ID.

    Returns:
        Bounty instance or None.
    """
    return db.query(Bounty).filter(Bounty.id == bounty_id).first()


def claim_bounty(
    db: Session,
    bounty: Bounty,
    claimer_name: str,
    claimer_callback_url: Optional[str] = None,
) -> str:
    """Claim a bounty. Returns the claimer_secret plaintext.

    Caller must check bounty.status == OPEN before calling.

    Args:
        db: Database session.
        bounty: The bounty to claim.
        claimer_name: Name of the claiming agent.
        claimer_callback_url: Optional webhook URL for the claimer.

    Returns:
        The plaintext claimer secret token.
    """
    secret_token, secret_hash = generate_secret()

    bounty.status = BountyStatus.CLAIMED
    bounty.claimed_by = sanitize_text(claimer_name)
    bounty.claimer_callback_url = claimer_callback_url
    bounty.claimer_secret_hash = secret_hash
    bounty.claimed_at = datetime.utcnow()
    db.commit()
    db.refresh(bounty)
    return secret_token


def fulfill_bounty(db: Session, bounty: Bounty, acp_job_id: Optional[str] = None) -> Bounty:
    """Mark a bounty as fulfilled.

    Args:
        db: Database session.
        bounty: The bounty to fulfill.
        acp_job_id: Optional ACP job ID.

    Returns:
        The updated bounty.
    """
    bounty.status = BountyStatus.FULFILLED
    bounty.acp_job_id = acp_job_id
    bounty.fulfilled_at = datetime.utcnow()
    db.commit()
    db.refresh(bounty)
    return bounty


def cancel_bounty(db: Session, bounty: Bounty) -> Bounty:
    """Cancel a bounty.

    Args:
        db: Database session.
        bounty: The bounty to cancel.

    Returns:
        The updated bounty.
    """
    bounty.status = BountyStatus.CANCELLED
    db.commit()
    db.refresh(bounty)
    return bounty


def get_platform_stats(db: Session) -> dict[str, int]:
    """Get bounty platform statistics.

    Args:
        db: Database session.

    Returns:
        Dict with bounty count stats.
    """
    return {
        "total_bounties": db.query(Bounty).count(),
        "open_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.OPEN).count(),
        "matched_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.MATCHED).count(),
        "fulfilled_bounties": db.query(Bounty).filter(Bounty.status == BountyStatus.FULFILLED).count(),
    }


def check_rate_limit(db: Session, poster_name: str, max_per_hour: int = 5) -> Optional[str]:
    """Check if a poster has exceeded the bounty creation rate limit.

    Args:
        db: Database session.
        poster_name: Name of the poster.
        max_per_hour: Maximum bounties per hour.

    Returns:
        Error message string or None if within limit.
    """
    one_hour_ago = datetime.utcnow() - timedelta(hours=1)
    recent_count = (
        db.query(Bounty)
        .filter(Bounty.poster_name == poster_name, Bounty.created_at >= one_hour_ago)
        .count()
    )
    if recent_count >= max_per_hour:
        return f"Rate limit exceeded: {poster_name} has created {recent_count} bounties in the last hour. Max {max_per_hour} per hour."
    return None
