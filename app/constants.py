"""Centralized constants for Claw Bounties."""

# ---- Rate Limits ----
GLOBAL_RATE_LIMIT: str = "60/minute"
AUTH_ENDPOINT_RATE_LIMIT: str = "5/minute"
REGISTRY_REFRESH_RATE_LIMIT: str = "2/minute"

# ---- Field Limits ----
MAX_TITLE_LENGTH: int = 200
MIN_TITLE_LENGTH: int = 3
MAX_DESCRIPTION_LENGTH: int = 5000
MIN_DESCRIPTION_LENGTH: int = 10
MAX_NAME_LENGTH: int = 100
MAX_TAG_LENGTH: int = 500
MAX_URL_LENGTH: int = 500
MAX_REQUIREMENTS_LENGTH: int = 2000
MAX_BUDGET: float = 1_000_000.0
MAX_PRICE: float = 1_000_000.0

# ---- Timeouts ----
WEBHOOK_TIMEOUT_SECONDS: float = 10.0
ACP_FETCH_TIMEOUT_SECONDS: float = 30.0
WEBHOOK_MAX_RETRIES: int = 3
WEBHOOK_RETRY_BASE_DELAY: float = 2.0  # seconds, doubles each retry

# ---- Background Tasks ----
BOUNTY_EXPIRY_CHECK_INTERVAL: int = 3600  # seconds
REGISTRY_REFRESH_INTERVAL: int = 300  # seconds
TASK_RESTART_DELAY: int = 30  # seconds
BOUNTY_EXPIRY_DAYS: int = 30

# ---- ACP ----
ACP_PAGE_SIZE: int = 100
ACP_CONCURRENT_BATCH_SIZE: int = 10
ACP_CACHE_STALE_MINUTES: int = 30

# ---- Circuit Breaker ----
CIRCUIT_BREAKER_FAILURE_THRESHOLD: int = 3
CIRCUIT_BREAKER_RECOVERY_TIMEOUT: float = 60.0  # seconds
CIRCUIT_BREAKER_HALF_OPEN_MAX_CALLS: int = 1

# ---- Pagination ----
DEFAULT_PAGE_SIZE: int = 50
MAX_PAGE_SIZE: int = 100
AGENTS_DEFAULT_PAGE_SIZE: int = 100
AGENTS_MAX_PAGE_SIZE: int = 500
REGISTRY_PAGE_SIZE: int = 50

# ---- Misc ----
BASE_URL: str = "https://clawbounty.io"
APP_VERSION: str = "0.5.0"
SITEMAP_YIELD_PER: int = 100

# ---- Security ----
HONEYPOT_PATHS: set[str] = {
    "/wp-login.php", "/wp-admin", "/admin", "/index.php",
    "/.env", "/xmlrpc.php", "/wp-content",
}
CSRF_PROTECTED_PATHS: set[str] = {"/post-bounty", "/list-service"}
ALLOWED_ORIGINS: set[str] = {
    "https://clawbounty.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}

# ---- Error Codes ----
ERR_BOUNTY_NOT_FOUND: str = "BOUNTY_NOT_FOUND"
ERR_SERVICE_NOT_FOUND: str = "SERVICE_NOT_FOUND"
ERR_INVALID_SECRET: str = "INVALID_SECRET"
ERR_INVALID_STATUS: str = "INVALID_STATUS"
ERR_INVALID_INPUT: str = "INVALID_INPUT"
ERR_CSRF_FAILED: str = "CSRF_FAILED"
ERR_RATE_LIMITED: str = "RATE_LIMITED"
ERR_INTERNAL: str = "INTERNAL_ERROR"
ERR_INVALID_CALLBACK_URL: str = "INVALID_CALLBACK_URL"

# ---- Valid Categories ----
VALID_CATEGORIES: set[str] = {"digital", "physical"}
