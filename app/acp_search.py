"""ACP Registry Search — search and categorize cached agents with inverted index."""
import logging
import re
from typing import Any, Dict, List, Optional

from app.acp_cache import get_cached_agents

logger = logging.getLogger(__name__)

# Inverted index: token -> set of agent indices
_inverted_index: Dict[str, set[int]] = {}
_indexed_agents: List[Dict[str, Any]] = []


def _tokenize(text: str) -> set[str]:
    """Tokenize text into lowercase alphanumeric tokens."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def rebuild_inverted_index(agents: List[Dict[str, Any]]) -> None:
    """Rebuild the inverted index from a list of agents."""
    global _inverted_index, _indexed_agents
    _indexed_agents = agents
    _inverted_index = {}
    for idx, agent in enumerate(agents):
        text = "%s %s" % (agent.get("name", ""), agent.get("description", ""))
        for job in agent.get("job_offerings", []):
            text += " %s %s" % (job.get("name", ""), job.get("description", ""))
        for token in _tokenize(text):
            if token not in _inverted_index:
                _inverted_index[token] = set()
            _inverted_index[token].add(idx)
    logger.info("Rebuilt inverted index: %s tokens, %s agents", len(_inverted_index), len(agents))


def categorize_agents(agents: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Categorize agents into products vs services."""
    product_keywords = [
        "3d print", "laser cut", "fabricat", "cnc", "mill",
        "shipping", "physical", "hardware", "manufacture",
        "printer", "maker", "craft", "build",
    ]

    products: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []

    for agent in agents:
        text = "%s %s" % (agent.get("name", ""), agent.get("description", ""))
        text = text.lower()
        for job in agent.get("job_offerings", []):
            text += " %s %s" % (job.get("name", ""), job.get("description", ""))

        is_product = any(kw in text for kw in product_keywords)
        if is_product:
            products.append(agent)
        else:
            services.append(agent)

    return {"products": products, "services": services}


def search_agents(query: str) -> List[Dict[str, Any]]:
    """Search cached agents using inverted index (fast) with fallback to linear scan."""
    agents = get_cached_agents()["agents"]

    # Use inverted index if available and built for current agents
    if _indexed_agents is agents and _inverted_index:
        tokens = _tokenize(query)
        if not tokens:
            return []
        # Intersect sets for all query tokens
        result_indices: Optional[set[int]] = None
        for token in tokens:
            matching = set()
            # Also match partial tokens (substring matching)
            for idx_token, idx_set in _inverted_index.items():
                if token in idx_token:
                    matching |= idx_set
            if result_indices is None:
                result_indices = matching
            else:
                result_indices &= matching
            if not result_indices:
                return []
        return [_indexed_agents[i] for i in sorted(result_indices or set())]

    # Fallback: linear scan (when index not yet built)
    query_lower = query.lower()
    results: list[dict[str, Any]] = []
    for agent in agents:
        text = "%s %s" % (agent.get("name", ""), agent.get("description", ""))
        text = text.lower()
        for job in agent.get("job_offerings", []):
            text += " %s %s" % (job.get("name", ""), job.get("description", ""))
        if query_lower in text:
            results.append(agent)
    return results


def get_agent_by_wallet(wallet: str) -> Optional[Dict[str, Any]]:
    """Find an agent by wallet address."""
    agents = get_cached_agents()["agents"]
    for agent in agents:
        if agent.get("wallet_address", "").lower() == wallet.lower():
            return agent
    return None
