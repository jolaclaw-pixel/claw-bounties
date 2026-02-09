"""Tests for bounty CRUD via the v1 API."""
from tests.conftest import create_test_bounty


def test_create_bounty_json(client):
    data = create_test_bounty(client)
    assert data["action"] == "posted"
    assert data["poster_secret"]
    assert data["bounty"]["id"]


def test_list_bounties(client):
    r = client.get("/api/v1/bounties/")
    assert r.status_code == 200
    data = r.json()
    # Backward compat
    assert "bounties" in data
    assert "total" in data
    # New envelope
    assert "data" in data
    assert "meta" in data
    assert "total" in data["meta"]
    assert "page" in data["meta"]
    assert "per_page" in data["meta"]


def test_list_open_bounties(client):
    r = client.get("/api/v1/bounties/open")
    assert r.status_code == 200
    data = r.json()
    assert "open_bounties" in data
    assert "count" in data


def test_get_bounty(client):
    data = create_test_bounty(client, title="Get Test Bounty")
    bounty_id = data["bounty"]["id"]
    r2 = client.get(f"/api/v1/bounties/{bounty_id}")
    assert r2.status_code == 200
    assert r2.json()["title"] == "Get Test Bounty"
    # Check ETag header
    assert "ETag" in r2.headers


def test_get_bounty_etag_304(client):
    data = create_test_bounty(client)
    bounty_id = data["bounty"]["id"]
    r1 = client.get(f"/api/v1/bounties/{bounty_id}")
    etag = r1.headers["ETag"]
    r2 = client.get(f"/api/v1/bounties/{bounty_id}", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_get_bounty_not_found(client):
    r = client.get("/api/v1/bounties/99999")
    assert r.status_code == 404
    data = r.json()
    assert "detail" in data


def test_create_bounty_invalid_title_too_short(client):
    r = client.post(
        "/api/v1/bounties/",
        json={"title": "AB", "description": "A test bounty for testing", "budget": 100, "poster_name": "agent"},
    )
    assert r.status_code == 422


def test_create_bounty_negative_budget(client):
    r = client.post(
        "/api/v1/bounties/",
        json={"title": "Test Bounty", "description": "A test bounty for testing", "budget": -10, "poster_name": "agent"},
    )
    assert r.status_code == 422


def test_create_bounty_invalid_category(client):
    r = client.post(
        "/api/v1/bounties/",
        json={"title": "Test Bounty", "description": "A test bounty for testing", "budget": 100, "poster_name": "agent", "category": "invalid"},
    )
    assert r.status_code == 422


def test_list_bounties_with_filters(client):
    create_test_bounty(client, title="Filtered Bounty", category="digital", budget=200)
    r = client.get("/api/v1/bounties/", params={"category": "digital", "min_budget": 150})
    assert r.status_code == 200


def test_list_bounties_with_search(client):
    create_test_bounty(client, title="Unique Searchable Title")
    r = client.get("/api/v1/bounties/", params={"search": "Unique Searchable"})
    assert r.status_code == 200
