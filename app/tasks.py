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
    """Run a coroutine forever, restarting on crash with a delay.

    Args:
        name: Human-readable task name for logging.
        coro_fn: Async callable to run in a loop.
        *args: Arguments forwarded to coro_fn.
    """
    while True:
        try:
            await coro_fn(*args)
        except Exception as e:
            logger.error(f"Task {name} crashed: {e}, restarting in {TASK_RESTART_DELAY}s...")
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
                logger.info(f"Auto-cancelled expired bounty #{bounty.id}: {bounty.title}")
            if expired:
                db.commit()
                logger.info(f"Expired {len(expired)} bounties")
        except Exception as e:
            logger.error(f"Bounty expiration task failed: {e}")
        finally:
            if db:
                db.close()


async def periodic_registry_refresh() -> None:
    """Background task to refresh ACP registry every 5 minutes and rebuild sitemap."""
    from app.acp_registry import refresh_cache

    while True:
        await asyncio.sleep(REGISTRY_REFRESH_INTERVAL)
        try:
            logger.info("Periodic ACP registry refresh starting...")
            await refresh_cache()
            logger.info("Periodic ACP registry refresh complete")

            # Rebuild sitemap after registry refresh
            try:
                from app.routers.misc import build_sitemap, set_sitemap_cache
                sitemap = await build_sitemap()
                set_sitemap_cache(sitemap)
                logger.info("Sitemap rebuilt after registry refresh")
            except Exception as e:
                logger.warning(f"Sitemap rebuild failed: {e}")

        except Exception as e:
            logger.error(f"Periodic refresh failed: {e}")
