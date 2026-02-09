"""Tests for backward compatibility redirects."""


def test_compat_bounties_redirect(client):
    """Old /api/bounties/ paths should redirect to /api/v1/bounties/."""
    r = client.get("/api/bounties/open", follow_redirects=False)
    assert r.status_code == 307
    assert "/api/v1/bounties/open" in r.headers["location"]
    assert r.headers.get("Deprecation") == "true"


def test_compat_services_redirect(client):
    """Old /api/services/ paths should redirect to /api/v1/services/."""
    r = client.get("/api/services/", follow_redirects=False)
    assert r.status_code == 307
    assert "/api/v1/services/" in r.headers["location"]
    assert r.headers.get("Deprecation") == "true"
