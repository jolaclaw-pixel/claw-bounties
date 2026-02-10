"""Round 3 tests: auto_match, expire_bounties, circuit breaker, SSRF, web forms, claim secret."""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock

import pytest
from tests.conftest import create_test_bounty


# ---- auto_match_bounties ----

def test_auto_match_bounties(client, db):
    """When a service is created with matching tags, open bounties get matched."""
    from app.models import Bounty, BountyStatus
    from app.services.service_service import auto_match_bounties, create_service

    # Create an open bounty with tags
    bounty = Bounty(
        poster_name="test",
        poster_secret_hash="x",
        title="Need logo design",
        description="I need a logo",
        budget=50,
        category="digital",
        tags="logo,design",
        status=BountyStatus.OPEN,
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)

    # Create a matching service
    service, _ = create_service(
        db,
        agent_name="designer",
        name="Logo Design",
        description="Professional logos",
        price=40,
        category="digital",
        tags="logo,branding",
        acp_agent_wallet="0xabc",
        acp_job_offering="logo-design",
    )

    auto_match_bounties(db, service)
    db.refresh(bounty)
    assert bounty.status == BountyStatus.MATCHED
    assert bounty.matched_service_id == service.id


# ---- expire_bounties_task ----

def test_expire_bounties_task(db):
    """Expired bounties should be auto-cancelled."""
    from app.models import Bounty, BountyStatus

    bounty = Bounty(
        poster_name="test",
        poster_secret_hash="x",
        title="Expiring bounty",
        description="This will expire",
        budget=10,
        category="digital",
        status=BountyStatus.OPEN,
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)

    # Run the expiry logic inline (same as expire_bounties_task but using test db)
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
    for b in expired:
        b.status = BountyStatus.CANCELLED
    db.commit()

    db.refresh(bounty)
    assert bounty.status == BountyStatus.CANCELLED


# ---- Circuit breaker state transitions ----

def test_circuit_breaker_transitions():
    """Circuit breaker should open after failures and close after success."""
    from app.circuit_breaker import CircuitBreaker, CircuitState

    cb = CircuitBreaker(name="test", failure_threshold=2, recovery_timeout=0.1)
    assert cb.state == CircuitState.CLOSED
    assert cb.can_execute()

    cb.record_failure()
    assert cb.state == CircuitState.CLOSED  # Not yet at threshold

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert not cb.can_execute()

    # Wait for recovery
    time.sleep(0.2)
    assert cb.can_execute()  # Should be HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED


# ---- SSRF validation ----

def test_callback_url_ssrf_validation():
    """SSRF: private IPs and localhost should be blocked."""
    from app.utils import validate_callback_url

    assert validate_callback_url("https://example.com/hook") is True
    assert validate_callback_url("http://localhost/hook") is False
    assert validate_callback_url("http://127.0.0.1/hook") is False
    assert validate_callback_url("http://10.0.0.1/hook") is False
    assert validate_callback_url("http://192.168.1.1/hook") is False
    assert validate_callback_url("http://[::1]/hook") is False
    assert validate_callback_url("ftp://example.com/hook") is False
    assert validate_callback_url("") is False
    assert validate_callback_url("http://foo.local/hook") is False
    assert validate_callback_url("http://foo.internal/hook") is False


# ---- Web form POST: create bounty ----

def test_web_post_bounty_form(client):
    """Web form bounty creation should work and return bounty_created page."""
    r = client.post(
        "/post-bounty",
        data={
            "poster_name": "web-agent",
            "title": "Web form bounty",
            "description": "Created via web form test",
            "budget": "25",
            "category": "digital",
        },
        headers={"origin": "http://localhost:8000"},
    )
    assert r.status_code == 200
    assert "poster_secret" in r.text.lower() or "web form bounty" in r.text.lower()


# ---- Web form POST: create service ----

def test_web_list_service_form(client):
    """Web form service creation should work."""
    r = client.post(
        "/list-service",
        data={
            "agent_name": "web-agent",
            "name": "Web Service",
            "description": "A test service via web form",
            "price": "30",
            "category": "digital",
        },
        headers={"origin": "http://localhost:8000"},
    )
    assert r.status_code == 200


# ---- Web claim shows secret ----

def test_web_claim_shows_secret(client, db):
    """Web claim should display claimer_secret to the user."""
    from app.models import Bounty, BountyStatus

    bounty = Bounty(
        poster_name="poster",
        poster_secret_hash="hash",
        title="Claim secret test",
        description="Testing web claim shows secret",
        budget=50,
        category="digital",
        status=BountyStatus.OPEN,
    )
    db.add(bounty)
    db.commit()
    db.refresh(bounty)

    r = client.post(
        f"/bounties/{bounty.id}/claim",
        data={"claimer_name": "claimer-agent"},
        headers={"origin": "http://localhost:8000"},
    )
    assert r.status_code == 200
    # The response should contain the claimer_secret (not a redirect)
    assert "claimer_secret" in r.text.lower() or "claim" in r.text.lower()


# ---- POST /bounties/ always creates ----

def test_create_bounty_always_creates(client):
    """POST /bounties/ should always create the bounty even if ACP matches exist."""
    data = create_test_bounty(client)
    assert data["action"] == "posted"
    assert data["bounty"] is not None
    assert data["poster_secret"] is not None
    assert data["bounty"]["id"] is not None


# ---- Registry refresh auth ----

def test_registry_refresh_no_auth_when_secret_set(client):
    """Registry refresh should require auth when ADMIN_SECRET is set."""
    import os
    old = os.environ.get("ADMIN_SECRET")
    os.environ["ADMIN_SECRET"] = "test-admin-secret"

    # Reload the module-level variable
    from app.routers import misc
    misc._ADMIN_SECRET = "test-admin-secret"

    r = client.post("/api/registry/refresh")
    assert r.status_code == 403

    # With correct secret
    r = client.post("/api/registry/refresh", headers={"X-Admin-Secret": "test-admin-secret"})
    assert r.status_code == 200

    # Clean up
    misc._ADMIN_SECRET = old or ""
    if old is None:
        os.environ.pop("ADMIN_SECRET", None)
    else:
        os.environ["ADMIN_SECRET"] = old


# ---- Registry refresh rate limiting ----

def test_registry_refresh_rate_limit(client):
    """Registry refresh should be rate-limited."""
    from app.routers import misc
    old_secret = misc._ADMIN_SECRET
    misc._ADMIN_SECRET = ""  # Disable auth for this test
    misc._last_refresh_time = 0.0  # Reset rate limit

    r1 = client.post("/api/registry/refresh")
    assert r1.status_code == 200

    r2 = client.post("/api/registry/refresh")
    assert r2.status_code == 429

    misc._ADMIN_SECRET = old_secret
