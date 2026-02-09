"""Tests for error handling — structured errors, no info leak."""


def test_api_404_structured(client):
    """API 404 should return structured error with code."""
    r = client.get("/api/v1/bounties/99999")
    assert r.status_code == 404
    data = r.json()
    assert "detail" in data
    # Structured error has detail as dict
    detail = data["detail"]
    if isinstance(detail, dict):
        assert "code" in detail
        assert "request_id" in detail


def test_api_error_no_stack_trace(client):
    """API errors should not leak stack traces or internal details."""
    r = client.get("/api/v1/bounties/99999")
    text = r.text
    assert "Traceback" not in text
    assert "File" not in text


def test_request_id_in_response(client):
    """Every response should have X-Request-ID header."""
    r = client.get("/health")
    assert "X-Request-ID" in r.headers


def test_security_headers(client):
    """Responses should have security headers."""
    r = client.get("/health")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in r.headers


def test_honeypot_paths(client):
    """Scanner paths should return 404."""
    for path in ["/wp-login.php", "/wp-admin", "/.env"]:
        r = client.get(path)
        assert r.status_code == 404
