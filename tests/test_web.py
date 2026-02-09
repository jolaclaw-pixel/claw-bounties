"""Tests for web (HTML) routes."""


def test_home_page(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_bounties_page(client):
    r = client.get("/bounties")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_services_page(client):
    r = client.get("/services")
    assert r.status_code == 200


def test_post_bounty_form(client):
    r = client.get("/post-bounty")
    assert r.status_code == 200


def test_list_service_form(client):
    r = client.get("/list-service")
    assert r.status_code == 200


def test_docs_page(client):
    r = client.get("/docs")
    assert r.status_code == 200


def test_success_stories_page(client):
    r = client.get("/success-stories")
    assert r.status_code == 200


def test_registry_page(client):
    r = client.get("/registry")
    assert r.status_code == 200


def test_offline_page(client):
    r = client.get("/offline.html")
    assert r.status_code == 200


def test_bounty_detail_not_found(client):
    r = client.get("/bounties/99999")
    assert r.status_code == 404
