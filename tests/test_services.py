"""Tests for service CRUD via the v1 API."""


def test_create_service(client):
    r = client.post(
        "/api/v1/services/",
        json={
            "agent_name": "test-agent",
            "name": "Test Service",
            "description": "A test service",
            "price": 50.0,
            "category": "digital",
        },
    )
    assert r.status_code == 201
    data = r.json()
    assert "service" in data
    assert "agent_secret" in data
    assert data["service"]["name"] == "Test Service"


def test_list_services(client):
    r = client.get("/api/v1/services/")
    assert r.status_code == 200
    data = r.json()
    # Backward compat
    assert "services" in data
    assert "total" in data
    # New envelope
    assert "data" in data
    assert "meta" in data


def test_get_service(client):
    create_r = client.post(
        "/api/v1/services/",
        json={"agent_name": "a", "name": "GetTest", "description": "desc", "price": 10},
    )
    sid = create_r.json()["service"]["id"]
    r = client.get(f"/api/v1/services/{sid}")
    assert r.status_code == 200
    assert "ETag" in r.headers


def test_get_service_not_found(client):
    r = client.get("/api/v1/services/99999")
    assert r.status_code == 404


def test_update_service(client):
    create_r = client.post(
        "/api/v1/services/",
        json={"agent_name": "a", "name": "UpdateTest", "description": "desc", "price": 10},
    )
    data = create_r.json()
    sid = data["service"]["id"]
    secret = data["agent_secret"]
    r = client.put(f"/api/v1/services/{sid}", json={"agent_secret": secret, "name": "Updated Name"})
    assert r.status_code == 200
    assert r.json()["name"] == "Updated Name"


def test_update_service_wrong_secret(client):
    create_r = client.post(
        "/api/v1/services/",
        json={"agent_name": "a", "name": "SecretTest", "description": "desc", "price": 10},
    )
    sid = create_r.json()["service"]["id"]
    r = client.put(f"/api/v1/services/{sid}", json={"agent_secret": "wrong", "name": "Hack"})
    assert r.status_code == 403


def test_deactivate_service(client):
    create_r = client.post(
        "/api/v1/services/",
        json={"agent_name": "a", "name": "DeleteTest", "description": "desc", "price": 10},
    )
    data = create_r.json()
    sid = data["service"]["id"]
    secret = data["agent_secret"]
    r = client.request("DELETE", f"/api/v1/services/{sid}", json={"agent_secret": secret})
    assert r.status_code == 200
    assert r.json()["message"] == "Service deactivated"
