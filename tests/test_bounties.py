"""Tests for bounty CRUD via the v1 API."""


def test_create_bounty_json(client):
    r = client.post(
        "/api/v1/bounties/",
        json={
            "title": "Test Bounty",
            "description": "A test bounty for testing",
            "budget": 100.0,
            "poster_name": "test-agent",
            "category": "digital",
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["action"] == "posted"
    assert data["poster_secret"]
    assert data["bounty"]["id"]


def test_list_bounties(client):
    r = client.get("/api/v1/bounties/")
    assert r.status_code == 200
    data = r.json()
    assert "bounties" in data
    assert "total" in data


def test_list_open_bounties(client):
    r = client.get("/api/v1/bounties/open")
    assert r.status_code == 200
    data = r.json()
    assert "open_bounties" in data


def test_get_bounty(client):
    # Create first
    r = client.post(
        "/api/v1/bounties/",
        json={"title": "Get Test Bounty", "description": "A description here", "budget": 50, "poster_name": "agent"},
    )
    bounty_id = r.json()["bounty"]["id"]

    r2 = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r2.status_code == 200
    assert r2.json()["title"] == "Get Test Bounty"


def test_get_bounty_not_found(client):
    r = client.get("/api/v1/bounties/99999")
    assert r.status_code == 404
    assert "detail" in r.json()
