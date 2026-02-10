"""ACP Registry Fetcher — pulls agents from Virtuals Protocol ACP API."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.constants import ACP_CONCURRENT_BATCH_SIZE, ACP_FETCH_TIMEOUT_SECONDS, ACP_PAGE_SIZE

logger = logging.getLogger(__name__)

ACPX_API_BASE: str = "https://acpx.virtuals.io/api/agents"


async def fetch_agents_page(page: int = 1, page_size: int = ACP_PAGE_SIZE) -> Dict[str, Any]:
    """Fetch a single page of agents from acpx.virtuals.io API.

    Args:
        page: Page number to fetch.
        page_size: Number of agents per page.

    Returns:
        Raw API response dict.
    """
    try:
        async with httpx.AsyncClient(timeout=ACP_FETCH_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                ACPX_API_BASE,
                params={"pagination[page]": page, "pagination[pageSize]": page_size},
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error("Error fetching page %s: %s", page, e)
        return {"data": [], "meta": {"pagination": {"total": 0}}}


def parse_agent(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse agent data from acpx.virtuals.io API format.

    Args:
        data: Raw agent data dict from the API.

    Returns:
        Parsed agent dict or None if invalid.
    """
    try:
        name = data.get("name", "Unknown")
        if not name or name == "Unknown":
            return None

        offerings: list[dict[str, Any]] = []
        for offering in data.get("offerings", []):
            offerings.append({
                "name": offering.get("name", ""),
                "price": offering.get("priceUsd") or offering.get("price"),
                "price_type": "fixed",
                "description": "",
            })

        for job in data.get("jobs", []):
            job_name = job.get("name", "")
            if not any(o["name"] == job_name for o in offerings):
                price_v2 = job.get("priceV2", {})
                offerings.append({
                    "name": job_name,
                    "price": job.get("price"),
                    "price_type": price_v2.get("type", "fixed"),
                    "description": (job.get("description", "") or "")[:200],
                })

        metrics = data.get("metrics", {})

        return {
            "id": data.get("id"),
            "name": name,
            "wallet_address": data.get("walletAddress", ""),
            "description": data.get("description", ""),
            "category": data.get("category", ""),
            "cluster": data.get("cluster", ""),
            "twitter": data.get("twitterHandle", ""),
            "profile_pic": data.get("profilePic", ""),
            "job_offerings": offerings,
            "stats": {
                "total_jobs": metrics.get("successfulJobCount", 0),
                "success_rate": metrics.get("successRate", 0),
                "unique_buyers": metrics.get("uniqueBuyerCount", 0),
                "transaction_count": data.get("transactionCount", 0),
                "last_active": metrics.get("lastActiveAt"),
                "rating": metrics.get("rating"),
            },
            "status": {
                "online": metrics.get("isOnline", False),
                "graduated": data.get("hasGraduated", False),
            },
        }
    except Exception as e:
        logger.error("Error parsing agent: %s", e)
        return None


async def fetch_all_agents(cached_agents: list, cached_last_updated: Any, cached_total_count: int) -> Dict[str, Any]:
    """Fetch ALL agents from acpx.virtuals.io API (paginated).

    Uses circuit breaker to avoid hammering a failing API.

    Args:
        cached_agents: Current cached agents list (used if circuit breaker is open).
        cached_last_updated: Current cached last_updated value.
        cached_total_count: Current cached total_count value.

    Returns:
        Dict with agents list, last_updated, total_from_api, and errors.
    """
    from app.circuit_breaker import acp_circuit_breaker

    if not acp_circuit_breaker.can_execute():
        logger.warning("ACP circuit breaker is OPEN — skipping fetch")
        return {
            "agents": cached_agents,
            "last_updated": cached_last_updated,
            "total_from_api": cached_total_count,
            "errors": ["Circuit breaker open — using cached data"],
        }

    all_agents: List[Dict[str, Any]] = []
    errors: list[str] = []
    total: int = 0

    try:
        first_page = await fetch_agents_page(1, ACP_PAGE_SIZE)
        meta = first_page.get("meta", {}).get("pagination", {})
        total = meta.get("total", 0)
        total_pages = meta.get("pageCount", 1)

        logger.info("ACP Registry: %s total agents across %s pages", total, total_pages)

        for agent_data in first_page.get("data", []):
            parsed = parse_agent(agent_data)
            if parsed:
                all_agents.append(parsed)

        if total_pages > 1:
            for batch_start in range(2, total_pages + 1, ACP_CONCURRENT_BATCH_SIZE):
                batch_end = min(batch_start + ACP_CONCURRENT_BATCH_SIZE, total_pages + 1)
                tasks = [fetch_agents_page(p, ACP_PAGE_SIZE) for p in range(batch_start, batch_end)]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        errors.append(f"Page {batch_start + i}: {str(result)}")
                        continue
                    for agent_data in result.get("data", []):
                        parsed = parse_agent(agent_data)
                        if parsed:
                            all_agents.append(parsed)

        acp_circuit_breaker.record_success()
    except Exception as e:
        acp_circuit_breaker.record_failure()
        logger.error("ACP fetch failed: %s", e)
        errors.append(str(e))

    return {
        "agents": all_agents,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_from_api": total,
        "errors": errors if errors else None,
    }
