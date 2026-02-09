def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] in ("healthy", "degraded")
    assert "database" in data


def test_robots(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Sitemap" in r.text
