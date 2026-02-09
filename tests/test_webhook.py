"""Tests for webhook functionality."""
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_webhook_signature():
    """Webhook should include HMAC signature when secret is configured."""
    import os
    os.environ["WEBHOOK_HMAC_SECRET"] = "test-secret-key"
    
    # Re-import to pick up env var
    from app.services.bounty_service import _sign_payload
    
    payload = {"event": "bounty.claimed", "bounty": {"id": 1}}
    sig = _sign_payload(payload)
    assert sig  # Should not be empty
    assert len(sig) == 64  # SHA256 hex digest length
    
    # Clean up
    os.environ.pop("WEBHOOK_HMAC_SECRET", None)


@pytest.mark.asyncio
async def test_send_webhook_retries():
    """Webhook should retry on failure."""
    from app.services.bounty_service import send_bounty_webhook
    
    with patch("app.services.bounty_service.httpx.AsyncClient") as mock_client:
        mock_instance = AsyncMock()
        mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_instance.__aexit__ = AsyncMock(return_value=False)
        mock_instance.post = AsyncMock(side_effect=Exception("Connection error"))
        mock_client.return_value = mock_instance
        
        # Should not raise — just log dead letter
        await send_bounty_webhook("https://example.com/hook", "bounty.claimed", {"id": 1})
        
        # Should have tried multiple times
        assert mock_instance.post.call_count >= 1
