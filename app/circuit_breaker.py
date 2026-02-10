"""Simple circuit breaker implementation for external API calls."""
import logging
import time
from enum import Enum

from app.constants import (
    CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
)

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple circuit breaker state machine.

    Tracks consecutive failures and backs off exponentially when the
    failure threshold is reached.

    Args:
        name: Human-readable name for logging.
        failure_threshold: Number of consecutive failures before opening.
        recovery_timeout: Seconds to wait before transitioning to half-open.
    """

    def __init__(
        self,
        name: str = "default",
        failure_threshold: int = CIRCUIT_BREAKER_FAILURE_THRESHOLD,
        recovery_timeout: float = CIRCUIT_BREAKER_RECOVERY_TIMEOUT,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.state = CircuitState.CLOSED
        self.failure_count: int = 0
        self.last_failure_time: float = 0.0
        self.consecutive_recovery_timeout: float = recovery_timeout

    def can_execute(self) -> bool:
        """Check whether a call is allowed through the breaker.

        Returns:
            True if the call should proceed, False if the circuit is open.
        """
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.last_failure_time
            if elapsed >= self.consecutive_recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker '%s' state=HALF_OPEN action=transition", self.name)
                return True
            return False
        # HALF_OPEN — allow one call
        return True

    def record_success(self) -> None:
        """Record a successful call, resetting the breaker to closed."""
        if self.state != CircuitState.CLOSED:
            logger.info("Circuit breaker '%s' state=CLOSED action=recovered", self.name)
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.consecutive_recovery_timeout = self.recovery_timeout

    def record_failure(self) -> None:
        """Record a failed call, potentially opening the breaker."""
        self.failure_count += 1
        self.last_failure_time = time.time()

        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            # Exponential backoff on recovery timeout
            self.consecutive_recovery_timeout = min(
                self.consecutive_recovery_timeout * 2, 600.0
            )
            logger.warning(
                "Circuit breaker '%s' state=OPEN action=opened failures=%d recovery_timeout=%.0fs",
                self.name, self.failure_count, self.consecutive_recovery_timeout,
            )


# Singleton for ACP registry fetches
acp_circuit_breaker = CircuitBreaker(name="acp_registry")
