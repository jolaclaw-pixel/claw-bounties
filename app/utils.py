"""Utility functions for Claw Bounties."""
import ipaddress
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


def validate_callback_url(url: str) -> bool:
    """
    Validate a callback/webhook URL to prevent SSRF attacks.
    Blocks private IPs, localhost, and non-HTTP(S) schemes.
    
    Returns True if URL is safe, False otherwise.
    """
    if not url:
        return False
    
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    
    # Must be http or https
    if parsed.scheme not in ("http", "https"):
        return False
    
    hostname = parsed.hostname
    if not hostname:
        return False
    
    # Block localhost variants
    blocked_hostnames = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
    if hostname.lower() in blocked_hostnames:
        return False
    
    # Try to parse as IP address and check if private
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        # Not an IP address (it's a hostname) - check for suspicious patterns
        hostname_lower = hostname.lower()
        if hostname_lower.endswith(".local") or hostname_lower.endswith(".internal"):
            return False
    
    return True
