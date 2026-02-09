"""ACP Registry Fetcher — pulls all agents from Virtuals Protocol ACP.

Uses the acpx.virtuals.io API for comprehensive agent data.
Includes circuit breaker protection for resilient fetching.
"""
import asyncio
import json as json_module
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from app.constants import ACP_CONCURRENT_BATCH_SIZE, ACP_FETCH_TIMEOUT_SECONDS, ACP_PAGE_SIZE

logger = logging.getLogger(__name__)

ACPX_API_BASE: str = "https://acpx.virtuals.io/api/agents"
CACHE_FILE_PATH: str = os.getenv("ACP_CACHE_PATH", "/data/acp_cache.json")

# Cache for ACP agents
_acp_cache: Dict[str, Any] = {
    "agents": [],
    "last_updated": None,
    "error": None,
    "total_count": 0,
}


def _load_cache_from_file() -> bool:
    """Load ACP cache from JSON file.

    Returns:
        True if loaded successfully, False otherwise.
    """
    global _acp_cache
    try:
        if os.path.exists(CACHE_FILE_PATH):
            with open(CACHE_FILE_PATH, "r") as f:
                data = json_module.load(f)
            if data.get("agents"):
                _acp_cache = data
                logger.info(f"Loaded ACP cache from file: {len(data['agents'])} agents")
                return True
    except Exception as e:
        logger.warning(f"Failed to load ACP cache from file: {e}")
    return False


def _save_cache_to_file() -> None:
    """Persist ACP cache to JSON file."""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE_PATH), exist_ok=True)
        with open(CACHE_FILE_PATH, "w") as f:
            json_module.dump(_acp_cache, f)
        logger.info(f"Saved ACP cache to {CACHE_FILE_PATH}")
    except Exception as e:
        logger.warning(f"Failed to save ACP cache to file: {e}")


# Load from file on module import (before async refresh)
_load_cache_from_file()


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
        logger.error(f"Error fetching page {page}: {e}")
        return {"data": [], "meta": {"pagination": {"total": 0}}}


async def fetch_all_agents() -> Dict[str, Any]:
    """Fetch ALL agents from acpx.virtuals.io API (paginated).

    Uses circuit breaker to avoid hammering a failing API.

    Returns:
        Dict with agents list, last_updated, total_from_api, and errors.
    """
    from app.circuit_breaker import acp_circuit_breaker

    if not acp_circuit_breaker.can_execute():
        logger.warning("ACP circuit breaker is OPEN — skipping fetch")
        return {
            "agents": _acp_cache.get("agents", []),
            "last_updated": _acp_cache.get("last_updated"),
            "total_from_api": _acp_cache.get("total_count", 0),
            "errors": ["Circuit breaker open — using cached data"],
        }

    all_agents: List[Dict[str, Any]] = []
    errors: list[str] = []

    try:
        first_page = await fetch_agents_page(1, ACP_PAGE_SIZE)
        meta = first_page.get("meta", {}).get("pagination", {})
        total = meta.get("total", 0)
        total_pages = meta.get("pageCount", 1)

        logger.info(f"ACP Registry: {total} total agents across {total_pages} pages")

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
        logger.error(f"ACP fetch failed: {e}")
        errors.append(str(e))

    return {
        "agents": all_agents,
        "last_updated": datetime.utcnow().isoformat(),
        "total_from_api": total if 'total' in dir() else 0,
        "errors": errors if errors else None,
    }


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
        logger.error(f"Error parsing agent: {e}")
        return None


async def refresh_cache() -> Dict[str, Any]:
    """Refresh the ACP agent cache.

    Returns:
        The updated cache dict.
    """
    global _acp_cache

    result = await fetch_all_agents()
    if result["agents"]:  # Only update if we got data
        _acp_cache = {
            "agents": result["agents"],
            "last_updated": result["last_updated"],
            "total_count": len(result["agents"]),
            "error": result.get("errors"),
        }
        logger.info(f"ACP Cache refreshed: {len(result['agents'])} agents")
        _save_cache_to_file()
    else:
        logger.warning("ACP refresh returned no agents — keeping existing cache")

    return _acp_cache


def get_cached_agents() -> Dict[str, Any]:
    """Get cached agents (returns empty if not yet loaded).

    Returns:
        The current ACP cache dict.
    """
    return _acp_cache


async def get_cached_agents_async() -> Dict[str, Any]:
    """Get cached agents, fetching if cache is empty.

    Returns:
        The ACP cache dict, refreshed if needed.
    """
    global _acp_cache
    if not _acp_cache["agents"]:
        return await refresh_cache()
    return _acp_cache


def categorize_agents(agents: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Categorize agents into products vs services.

    Args:
        agents: List of parsed agent dicts.

    Returns:
        Dict with 'products' and 'services' lists.
    """
    product_keywords = [
        "3d print", "laser cut", "fabricat", "cnc", "mill",
        "shipping", "physical", "hardware", "manufacture",
        "printer", "maker", "craft", "build",
    ]

    products: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []

    for agent in agents:
        text = f"{agent.get('name', '')} {agent.get('description', '')}".lower()
        for job in agent.get("job_offerings", []):
            text += f" {job.get('name', '')} {job.get('description', '')}"

        is_product = any(kw in text for kw in product_keywords)
        if is_product:
            products.append(agent)
        else:
            services.append(agent)

    return {"products": products, "services": services}


def search_agents(query: str) -> List[Dict[str, Any]]:
    """Search cached agents by query string.

    Args:
        query: Search query to match against agent names, descriptions, and offerings.

    Returns:
        List of matching agent dicts.
    """
    agents = get_cached_agents()["agents"]
    query_lower = query.lower()

    results: list[dict[str, Any]] = []
    for agent in agents:
        text = f"{agent.get('name', '')} {agent.get('description', '')}".lower()
        for job in agent.get("job_offerings", []):
            text += f" {job.get('name', '')} {job.get('description', '')}"
        if query_lower in text:
            results.append(agent)

    return results


def get_agent_by_wallet(wallet: str) -> Optional[Dict[str, Any]]:
    """Find an agent by wallet address.

    Args:
        wallet: Wallet address to search for.

    Returns:
        Agent dict or None if not found.
    """
    agents = get_cached_agents()["agents"]
    for agent in agents:
        if agent.get("wallet_address", "").lower() == wallet.lower():
            return agent
    return None
