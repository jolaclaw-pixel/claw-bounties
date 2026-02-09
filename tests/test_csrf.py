"""Tests for CSRF protection."""


def test_csrf_blocks_bad_origin(client):
    """POST to CSRF-protected endpoint with bad origin should be rejected."""
    r = client.post(
        "/post-bounty",
        data={"poster_name": "a", "title": "t", "description": "d", "budget": "10", "category": "digital"},
        headers={"origin": "https://evil.com"},
    )
    assert r.status_code == 403
    assert "CSRF" in r.json().get("detail", "")


def test_csrf_allows_good_origin(client):
    """POST to CSRF-protected endpoint with valid origin should pass CSRF check."""
    r = client.post(
        "/post-bounty",
        data={"poster_name": "a", "title": "test title", "description": "test description long enough", "budget": "10", "category": "digital"},
        headers={"origin": "http://localhost:8000"},
    )
    # Should pass CSRF (may fail for other reasons like template rendering)
    assert r.status_code != 403
