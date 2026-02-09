"""Tests for health, robots, sitemap, and misc endpoints."""


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("healthy", "degraded", "warning")
    assert "database" in data
    assert "acp_cache" in data
    assert "request_id" in data


def test_robots(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap" in r.text
    assert "Disallow: /api/" in r.text


def test_sitemap(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "urlset" in r.text
    assert "clawbounty.io" in r.text
    assert "ETag" in r.headers


def test_favicon(client):
    r = client.get("/favicon.ico")
    assert r.status_code in (200, 204)


def test_skill_manifest(client):
    r = client.get("/api/skill")
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "claw-bounties"
    assert "endpoints" in data


def test_skill_json(client):
    r = client.get("/api/skill.json")
    assert r.status_code == 200
    assert r.json()["name"] == "claw-bounties"


def test_registry_endpoint(client):
    r = client.get("/api/registry")
    assert r.status_code == 200
    data = r.json()
    assert "products" in data
    assert "services" in data
    assert "total_agents" in data
