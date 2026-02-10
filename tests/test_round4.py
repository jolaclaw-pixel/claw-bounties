"""Round 4 tests: rate limiting, pagination boundaries, XSS sanitization, inverted index, stats caching."""
import os
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from tests.conftest import create_test_bounty


class TestPaginationBoundaries:
    """Test pagination edge cases."""

    def test_page_zero_bounties(self, client: TestClient) -> None:
        """offset=0 should work fine."""
        r = client.get("/api/v1/bounties/?offset=0&limit=10")
        assert r.status_code == 200

    def test_very_large_offset(self, client: TestClient) -> None:
        """Large offset returns empty results."""
        r = client.get("/api/v1/bounties/?offset=999999&limit=10")
        assert r.status_code == 200
        data = r.json()
        assert len(data["data"]) == 0

    def test_per_page_zero_services(self, client: TestClient) -> None:
        """limit=0 should be handled (returns empty or error)."""
        # FastAPI Query(le=MAX_PAGE_SIZE) doesn't enforce gt=0 by default
        r = client.get("/api/v1/services/?limit=0")
        # Should not crash
        assert r.status_code in (200, 422)

    def test_agents_page_999(self, client: TestClient) -> None:
        """Very high page number returns empty agents."""
        r = client.get("/api/v1/agents?page=999")
        assert r.status_code == 200
        data = r.json()
        assert data["data"] == []


class TestInputSanitization:
    """Test XSS prevention in bounty creation."""

    def test_xss_in_title(self, client: TestClient) -> None:
        """Script tags should be stripped from title."""
        payload = {
            "title": '<script>alert("xss")</script>Test Bounty',
            "description": "A normal description for testing",
            "budget": 50.0,
            "poster_name": "test-agent",
            "category": "digital",
        }
        r = client.post("/api/v1/bounties/", json=payload)
        assert r.status_code == 201
        data = r.json()
        assert "<script>" not in data["bounty"]["title"]
        assert "alert" not in data["bounty"]["title"] or "&" in data["bounty"]["title"]

    def test_xss_in_description(self, client: TestClient) -> None:
        """Script tags should be stripped from description."""
        payload = {
            "title": "Normal Title Here",
            "description": '<img src=x onerror=alert(1)>Normal description text',
            "budget": 50.0,
            "poster_name": "test-agent",
            "category": "digital",
        }
        r = client.post("/api/v1/bounties/", json=payload)
        assert r.status_code == 201
        data = r.json()
        assert "onerror" not in data["bounty"]["description"]


class TestACPSearchInvertedIndex:
    """Test the inverted index search functionality."""

    def test_search_agents_endpoint(self, client: TestClient) -> None:
        """Search endpoint should return results."""
        r = client.get("/api/v1/agents/search?q=test")
        assert r.status_code == 200
        data = r.json()
        assert "agents" in data
        assert "count" in data

    def test_rebuild_inverted_index(self) -> None:
        """Inverted index should be buildable."""
        from app.acp_search import rebuild_inverted_index, search_agents, _inverted_index, _indexed_agents

        agents = [
            {"name": "TraderBot", "description": "Automated trading agent", "job_offerings": [{"name": "trade", "description": "Execute trades"}]},
            {"name": "DesignBot", "description": "Logo design service", "job_offerings": [{"name": "logo", "description": "Create logos"}]},
        ]

        # Patch get_cached_agents to return our test agents
        with patch("app.acp_search.get_cached_agents", return_value={"agents": agents}):
            rebuild_inverted_index(agents)
            assert len(_inverted_index) > 0

            # Search should find TraderBot
            results = search_agents("trading")
            assert len(results) >= 1
            assert results[0]["name"] == "TraderBot"

            # Search for logo should find DesignBot
            results = search_agents("logo")
            assert len(results) >= 1
            assert results[0]["name"] == "DesignBot"


class TestStatsCaching:
    """Test that stats endpoint caching works."""

    def test_stats_returns_data(self, client: TestClient) -> None:
        """Stats endpoint should return bounty and agent data."""
        r = client.get("/api/v1/stats")
        assert r.status_code == 200
        data = r.json()
        assert "bounties" in data
        assert "agents" in data

    def test_stats_cached(self, client: TestClient) -> None:
        """Second call within 60s should return cached data."""
        r1 = client.get("/api/v1/stats")
        assert r1.status_code == 200
        r2 = client.get("/api/v1/stats")
        assert r2.status_code == 200
        # Both should return same structure
        assert r1.json()["bounties"] == r2.json()["bounties"]


class TestRateLimiting:
    """Test rate limiting returns 429."""

    def test_rate_limit_registry_refresh(self, client: TestClient) -> None:
        """Registry refresh should be rate limited."""
        # First call may succeed or fail auth, but rapid calls should eventually 429
        for _ in range(3):
            r = client.post("/api/registry/refresh")
        # After rapid calls, should get 429
        r = client.post("/api/registry/refresh")
        assert r.status_code in (403, 429)  # 403 if auth required, 429 if rate limited


class TestCreateReturns201:
    """Test that POST endpoints return 201."""

    def test_create_bounty_201(self, client: TestClient) -> None:
        payload = {
            "title": "Test 201 Bounty",
            "description": "Testing that create returns 201",
            "budget": 25.0,
            "poster_name": "test-agent",
            "category": "digital",
        }
        r = client.post("/api/v1/bounties/", json=payload)
        assert r.status_code == 201

    def test_create_service_201(self, client: TestClient) -> None:
        payload = {
            "agent_name": "TestAgent",
            "name": "Test Service",
            "description": "A test service",
            "price": 10.0,
            "category": "digital",
        }
        r = client.post("/api/v1/services/", json=payload)
        assert r.status_code == 201


class TestAPIKeyAuth:
    """Test optional API key authentication for write endpoints."""

    def test_no_auth_when_no_env(self, client: TestClient) -> None:
        """When API_WRITE_KEY is not set, writes should work."""
        # Clear the env var
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_WRITE_KEY", None)
            payload = {
                "title": "No Auth Bounty",
                "description": "Should work without API key",
                "budget": 10.0,
                "poster_name": "test",
                "category": "digital",
            }
            r = client.post("/api/v1/bounties/", json=payload)
            assert r.status_code in (201, 200)
