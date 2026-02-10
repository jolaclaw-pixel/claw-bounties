"""Background tasks for Claw Bounties: ACP refresh, bounty expiration."""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from app.constants import (
    BOUNTY_EXPIRY_CHECK_INTERVAL,
    REGISTRY_REFRESH_INTERVAL,
    TASK_RESTART_DELAY,
)
from app.database import SessionLocal
from app.models import Bounty, BountyStatus

logger = logging.getLogger(__name__)


async def supervised_task(name: str, coro_fn: Any, *args: Any) -> None:
    """Run a coroutine forever, restarting on crash with a delay."""
    while True:
        try:
            await coro_fn(*args)
        except Exception:
            logger.exception("Task %s crashed, restarting in %ss...", name, TASK_RESTART_DELAY)
            await asyncio.sleep(TASK_RESTART_DELAY)


async def expire_bounties_task() -> None:
    """Background task to auto-cancel expired bounties every hour."""
    while True:
        await asyncio.sleep(BOUNTY_EXPIRY_CHECK_INTERVAL)
        db = None
        try:
            db = SessionLocal()
            now = datetime.now(timezone.utc)
            expired = (
                db.query(Bounty)
                .filter(
                    Bounty.status.in_([BountyStatus.OPEN, BountyStatus.CLAIMED]),
                    Bounty.expires_at.isnot(None),
                    Bounty.expires_at <= now,
                )
                .all()
            )
            for bounty in expired:
                bounty.status = BountyStatus.CANCELLED
                logger.info("Auto-cancelled expired bounty #%s: %s", bounty.id, bounty.title)
            if expired:
                db.commit()
                logger.info("Expired %s bounties", len(expired))
        except Exception:
            logger.exception("Bounty expiration task failed")
        finally:
            if db:
                db.close()


async def periodic_registry_refresh() -> None:
    """Background task to refresh ACP registry every 5 minutes and rebuild sitemap if dirty."""
    from app.acp_registry import refresh_cache

    while True:
        await asyncio.sleep(REGISTRY_REFRESH_INTERVAL)
        try:
            logger.info("Periodic ACP registry refresh starting...")
            await refresh_cache()
            logger.info("Periodic ACP registry refresh complete")

            # Only rebuild sitemap if it's been invalidated
            try:
                from app.routers.misc import build_sitemap, is_sitemap_dirty, mark_sitemap_clean, set_sitemap_cache
                if is_sitemap_dirty():
                    sitemap = await build_sitemap()
                    set_sitemap_cache(sitemap)
                    mark_sitemap_clean()
                    logger.info("Sitemap rebuilt after registry refresh")
            except Exception:
                logger.exception("Sitemap rebuild failed")

        except Exception:
            logger.exception("Periodic refresh failed")
