"""ACP Registry Cache — persistence and in-memory cache management."""
import json as json_module
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

CACHE_FILE_PATH: str = os.getenv("ACP_CACHE_PATH", "/data/acp_cache.json")

# In-memory cache for ACP agents
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


def get_cached_agents() -> Dict[str, Any]:
    """Get cached agents (returns empty if not yet loaded).

    Returns:
        The current ACP cache dict.
    """
    return _acp_cache


def update_cache(agents: list, last_updated: Any, errors: Any = None) -> Dict[str, Any]:
    """Update the in-memory cache and persist to file.

    Args:
        agents: List of parsed agent dicts.
        last_updated: ISO timestamp of when the data was fetched.
        errors: Optional error list.

    Returns:
        The updated cache dict.
    """
    global _acp_cache
    _acp_cache = {
        "agents": agents,
        "last_updated": last_updated,
        "total_count": len(agents),
        "error": errors,
    }
    _save_cache_to_file()
    return _acp_cache
