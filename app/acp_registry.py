"""ACP Registry — facade module re-exporting from split submodules.

Maintains backward compatibility for all existing imports.
Actual logic lives in: acp_fetcher.py, acp_cache.py, acp_search.py.
"""
import logging
from typing import Any, Dict

from app.acp_cache import get_cached_agents, update_cache  # noqa: F401
from app.acp_fetcher import fetch_agents_page, fetch_all_agents, parse_agent  # noqa: F401
from app.acp_search import categorize_agents, get_agent_by_wallet, search_agents  # noqa: F401

logger = logging.getLogger(__name__)


async def refresh_cache() -> Dict[str, Any]:
    """Refresh the ACP agent cache.

    Returns:
        The updated cache dict.
    """
    cache = get_cached_agents()
    result = await fetch_all_agents(
        cached_agents=cache.get("agents", []),
        cached_last_updated=cache.get("last_updated"),
        cached_total_count=cache.get("total_count", 0),
    )
    if result["agents"]:
        updated = update_cache(result["agents"], result["last_updated"], result.get("errors"))
        logger.info(f"ACP Cache refreshed: {len(result['agents'])} agents")
        return updated
    else:
        logger.warning("ACP refresh returned no agents — keeping existing cache")
        return cache


async def get_cached_agents_async() -> Dict[str, Any]:
    """Get cached agents, fetching if cache is empty.

    Returns:
        The ACP cache dict, refreshed if needed.
    """
    cache = get_cached_agents()
    if not cache["agents"]:
        return await refresh_cache()
    return cache
