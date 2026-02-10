"""ACP Registry Search — search and categorize cached agents."""
import logging
from typing import Any, Dict, List, Optional

from app.acp_cache import get_cached_agents

logger = logging.getLogger(__name__)


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
