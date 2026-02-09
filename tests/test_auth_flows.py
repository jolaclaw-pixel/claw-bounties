"""Tests for auth flows: secret verification, claim, fulfill, cancel, unclaim."""
from tests.conftest import create_test_bounty


def _create_bounty(client):
    """Helper: create a bounty and return (bounty_id, poster_secret)."""
    data = create_test_bounty(client, title="Auth Test Bounty", poster_name="auth-test-agent")
    return data["bounty"]["id"], data["poster_secret"]


def test_secret_verification_valid(client):
    bounty_id, secret = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": secret})
    assert r.status_code == 200


def test_secret_verification_invalid(client):
    bounty_id, _ = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": "wrong-secret"})
    assert r.status_code == 403
    data = r.json()
    assert "detail" in data


def test_bounty_claim_flow(client):
    bounty_id, _ = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "claimer-agent"})
    assert r.status_code == 200
    data = r.json()
    assert data["bounty_id"] == bounty_id
    assert data["claimed_by"] == "claimer-agent"
    assert "claimer_secret" in data


def test_bounty_claim_already_claimed(client):
    bounty_id, _ = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "first"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "second"})
    assert r.status_code == 400


def test_bounty_unclaim_flow(client):
    bounty_id, _ = _create_bounty(client)
    claim_r = client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "unclaimer"})
    claimer_secret = claim_r.json()["claimer_secret"]
    r = client.post(f"/api/v1/bounties/{bounty_id}/unclaim", json={"claimer_secret": claimer_secret})
    assert r.status_code == 200
    assert r.json()["status"] == "open"


def test_bounty_unclaim_wrong_secret(client):
    bounty_id, _ = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "agent"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/unclaim", json={"claimer_secret": "bad"})
    assert r.status_code == 403


def test_bounty_fulfill_flow(client):
    bounty_id, secret = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "fulfiller"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/fulfill", json={"poster_secret": secret, "acp_job_id": "job-123"})
    assert r.status_code == 200


def test_bounty_fulfill_wrong_secret(client):
    bounty_id, _ = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "agent"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/fulfill", json={"poster_secret": "bad", "acp_job_id": "j1"})
    assert r.status_code == 403


def test_bounty_cancel_flow(client):
    bounty_id, secret = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": secret})
    assert r.status_code == 200


def test_bounty_cancel_fulfilled_fails(client):
    bounty_id, secret = _create_bounty(client)
    client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "agent"})
    client.post(f"/api/v1/bounties/{bounty_id}/fulfill", json={"poster_secret": secret, "acp_job_id": "j1"})
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": secret})
    assert r.status_code == 400


def test_get_missing_bounty_returns_404(client):
    r = client.get("/api/v1/bounties/99999")
    assert r.status_code == 404


def test_create_claim_fulfill_lifecycle(client):
    """Full lifecycle: create → claim → fulfill."""
    bounty_id, poster_secret = _create_bounty(client)
    # Verify open
    r = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r.json()["status"] == "open"
    # Claim
    claim_r = client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "worker"})
    assert claim_r.status_code == 200
    r = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r.json()["status"] == "claimed"
    # Fulfill
    fulfill_r = client.post(f"/api/v1/bounties/{bounty_id}/fulfill", json={"poster_secret": poster_secret, "acp_job_id": "acp-456"})
    assert fulfill_r.status_code == 200
    r = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r.json()["status"] == "fulfilled"


def test_create_cancel_lifecycle(client):
    """Lifecycle: create → cancel."""
    bounty_id, poster_secret = _create_bounty(client)
    r = client.post(f"/api/v1/bounties/{bounty_id}/cancel", json={"poster_secret": poster_secret})
    assert r.status_code == 200
    r = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r.json()["status"] == "cancelled"


def test_create_claim_unclaim_lifecycle(client):
    """Lifecycle: create → claim → unclaim."""
    bounty_id, _ = _create_bounty(client)
    claim_r = client.post(f"/api/v1/bounties/{bounty_id}/claim", json={"claimer_name": "temp"})
    claimer_secret = claim_r.json()["claimer_secret"]
    unclaim_r = client.post(f"/api/v1/bounties/{bounty_id}/unclaim", json={"claimer_secret": claimer_secret})
    assert unclaim_r.status_code == 200
    r = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r.json()["status"] == "open"
