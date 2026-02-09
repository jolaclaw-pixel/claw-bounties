"""Tests for agent API endpoints."""


def test_list_agents(client):
    r = client.get("/api/v1/agents")
    assert r.status_code == 200
    data = r.json()
    assert "agents" in data
    assert "count" in data
    # New envelope
    assert "data" in data
    assert "meta" in data


def test_search_agents(client):
    r = client.get("/api/v1/agents/search", params={"q": "trading"})
    assert r.status_code == 200
    data = r.json()
    assert data["query"] == "trading"
    assert "agents" in data


def test_search_agents_min_length(client):
    r = client.get("/api/v1/agents/search", params={"q": "a"})
    assert r.status_code == 422


def test_stats(client):
    r = client.get("/api/v1/stats")
    assert r.status_code == 200
    data = r.json()
    assert "bounties" in data
    assert "agents" in data
    assert "open" in data["bounties"]
    assert "total" in data["agents"]
