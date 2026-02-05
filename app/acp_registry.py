"""
ACP Registry Fetcher - Pulls all agents from Virtuals Protocol ACP
Uses the acpx.virtuals.io API for comprehensive agent data
"""
import httpx
import asyncio
from typing import List, Dict, Any, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

ACPX_API_BASE = "https://acpx.virtuals.io/api/agents"
PAGE_SIZE = 100  # Max per request

# Cache for ACP agents
_acp_cache: Dict[str, Any] = {
    "agents": [],
    "last_updated": None,
    "error": None,
    "total_count": 0
}


async def fetch_agents_page(page: int = 1, page_size: int = PAGE_SIZE) -> Dict[str, Any]:
    """Fetch a single page of agents from acpx.virtuals.io API."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                ACPX_API_BASE,
                params={
                    "pagination[page]": page,
                    "pagination[pageSize]": page_size
                }
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.error(f"Error fetching page {page}: {e}")
        return {"data": [], "meta": {"pagination": {"total": 0}}}


async def fetch_all_agents() -> Dict[str, Any]:
    """Fetch ALL agents from acpx.virtuals.io API (paginated)."""
    all_agents: List[Dict[str, Any]] = []
    errors = []
    
    # Get first page to determine total
    first_page = await fetch_agents_page(1, PAGE_SIZE)
    
    meta = first_page.get("meta", {}).get("pagination", {})
    total = meta.get("total", 0)
    total_pages = meta.get("pageCount", 1)
    
    logger.info(f"ACP Registry: {total} total agents across {total_pages} pages")
    
    # Process first page
    for agent_data in first_page.get("data", []):
        parsed = parse_agent(agent_data)
        if parsed:
            all_agents.append(parsed)
    
    # Fetch remaining pages concurrently (in batches to avoid overwhelming)
    if total_pages > 1:
        batch_size = 10
        for batch_start in range(2, total_pages + 1, batch_size):
            batch_end = min(batch_start + batch_size, total_pages + 1)
            tasks = [fetch_agents_page(p, PAGE_SIZE) for p in range(batch_start, batch_end)]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    errors.append(f"Page {batch_start + i}: {str(result)}")
                    continue
                
                for agent_data in result.get("data", []):
                    parsed = parse_agent(agent_data)
                    if parsed:
                        all_agents.append(parsed)
    
    return {
        "agents": all_agents,
        "last_updated": datetime.utcnow().isoformat(),
        "total_from_api": total,
        "errors": errors if errors else None
    }


def parse_agent(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse agent data from acpx.virtuals.io API format."""
    try:
        # Data is at top level (not nested in attributes)
        name = data.get("name", "Unknown")
        if not name or name == "Unknown":
            return None  # Skip agents without names
        
        # Extract job offerings from both 'offerings' and 'jobs' arrays
        offerings = []
        for offering in data.get("offerings", []):
            offerings.append({
                "name": offering.get("name", ""),
                "price": offering.get("priceUsd") or offering.get("price"),
                "price_type": "fixed",
                "description": ""
            })
        
        # Jobs have more detail
        for job in data.get("jobs", []):
            # Avoid duplicates
            job_name = job.get("name", "")
            if not any(o["name"] == job_name for o in offerings):
                price_v2 = job.get("priceV2", {})
                offerings.append({
                    "name": job_name,
                    "price": job.get("price"),
                    "price_type": price_v2.get("type", "fixed"),
                    "description": job.get("description", "")[:200] if job.get("description") else ""
                })
        
        # Extract metrics
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
                "rating": metrics.get("rating")
            },
            "status": {
                "online": metrics.get("isOnline", False),
                "graduated": data.get("hasGraduated", False)
            }
        }
    except Exception as e:
        logger.error(f"Error parsing agent: {e}")
        return None


async def refresh_cache() -> Dict[str, Any]:
    """Refresh the ACP agent cache."""
    global _acp_cache
    
    result = await fetch_all_agents()
    _acp_cache = {
        "agents": result["agents"],
        "last_updated": result["last_updated"],
        "total_count": len(result["agents"]),
        "error": result.get("errors")
    }
    
    logger.info(f"ACP Cache refreshed: {len(result['agents'])} agents")
    return _acp_cache


def get_cached_agents() -> Dict[str, Any]:
    """Get cached agents (returns empty if not yet loaded)."""
    global _acp_cache
    return _acp_cache


async def get_cached_agents_async() -> Dict[str, Any]:
    """Get cached agents, fetching if cache is empty."""
    global _acp_cache
    
    if not _acp_cache["agents"]:
        return await refresh_cache()
    
    return _acp_cache


def categorize_agents(agents: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Categorize agents into products vs services."""
    product_keywords = [
        "3d print", "laser cut", "fabricat", "cnc", "mill", 
        "shipping", "physical", "hardware", "manufacture",
        "printer", "maker", "craft", "build"
    ]
    
    products = []
    services = []
    
    for agent in agents:
        text = f"{agent.get('name', '')} {agent.get('description', '')}".lower()
        # Also check offerings
        for job in agent.get("job_offerings", []):
            text += f" {job.get('name', '')} {job.get('description', '')}"
        
        is_product = any(kw in text for kw in product_keywords)
        
        if is_product:
            products.append(agent)
        else:
            services.append(agent)
    
    return {
        "products": products,
        "services": services
    }


def search_agents(query: str) -> List[Dict[str, Any]]:
    """Search cached agents by query."""
    agents = get_cached_agents()["agents"]
    query_lower = query.lower()
    
    results = []
    for agent in agents:
        text = f"{agent.get('name', '')} {agent.get('description', '')}".lower()
        # Also search in job offering names
        for job in agent.get("job_offerings", []):
            text += f" {job.get('name', '')} {job.get('description', '')}"
        
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
