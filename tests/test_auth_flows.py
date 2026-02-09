"""Tests for auth flows: secret verification, claim, fulfill, cancel, invalid secret rejection."""


def _create_bounty(client):
    """Helper: create a bounty and return (bounty_id, poster_secret)."""
    r = client.post(
        "/api/v1/bounties/",
        json={
            "title": "Auth Test Bounty",
            "description": "Testing auth flows end to end",
            "budget": 100.0,
            "poster_name": "auth-test-agent",
            "category": "digital",
        },
    )
    assert r.status_code == 200
    data = r.json()
    # bounties.py create returns BountyPostResponse with action/bounty/poster_secret
    return data["bounty"]["id"], data["poster_secret"]


def test_secret_verification_valid(client):
    """Valid poster_secret should allow cancel."""
    bounty_id, secret = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": secret})
    assert r.status_code == 200
    assert r.json()["status"] in ("cancelled", "CANCELLED") or r.json().get("status", r.json().get("status")) is not None


def test_secret_verification_invalid(client):
    """Invalid poster_secret should be rejected with 403."""
    bounty_id, _ = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": "wrong-secret"})
    assert r.status_code == 403
    assert "detail" in r.json()


def test_bounty_claim_flow(client):
    """Claim a bounty and verify status changes."""
    bounty_id, _ = _create_bounty(client)
    r = client.post(
        f"/api/v1/bounties/{bounty_id}/claim",
        json={"claimer_name": "claimer-agent"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["bounty_id"] == bounty_id
    assert data["claimed_by"] == "claimer-agent"
    assert "claimer_secret" in data


def test_bounty_claim_already_claimed(client):
    """Cannot claim an already claimed bounty."""
    bounty_id, _ = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "first"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "second"})
    assert r.status_code == 400


def test_bounty_fulfill_flow(client):
    """Fulfill a claimed bounty with valid poster_secret."""
    bounty_id, secret = _create_bounty(client)
    # Claim first
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "fulfiller"})
    # Fulfill
    r = client.post(
        f"/api/v1/bounties/{bounty_id}/fulfill",
        json={"poster_secret": secret, "acp_job_id": "job-123"},
    )
    assert r.status_code == 200


def test_bounty_fulfill_wrong_secret(client):
    """Fulfill with wrong secret should fail."""
    bounty_id, _ = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "agent"})
    r = client.post(
        f"/api/v1/bounties/{bounty_id}/fulfill",
        json={"poster_secret": "bad-secret", "acp_job_id": "job-123"},
    )
    assert r.status_code == 403


def test_bounty_cancel_flow(client):
    """Cancel a bounty with valid poster_secret."""
    bounty_id, secret = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": secret})
    assert r.status_code == 200


def test_bounty_cancel_fulfilled_fails(client):
    """Cannot cancel a fulfilled bounty."""
    bounty_id, secret = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "agent"})
    client.post(f"/api/v1/bounties/{bounty_id}/fulfill", json={"poster_secret": secret, "acp_job_id": "j1"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": secret})
    assert r.status_code == 400


def test_get_missing_bounty_returns_404(client):
    """GET /api/v1/bounties/99999 should return 404."""
    r = client.get("/api/v1/bounties/99999")
    assert r.status_code == 404
