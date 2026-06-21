"""
Circuit breaker implementation.

States:
    CLOSED     — normal operation, failures counted.
    OPEN       — fast-fail, no calls go through.
    HALF_OPEN  — one probe call allowed to test recovery.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Callable


class CircuitBreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpen(Exception):
    """Raised when a call is attempted while the circuit breaker is OPEN."""


class CircuitBreaker:
    """
    Three-state circuit breaker.

    Args:
        failure_threshold: Number of consecutive failures before opening the circuit.
        reset_timeout:     Seconds to wait before transitioning OPEN → HALF_OPEN.
        half_open_max:     Maximum probe calls allowed in HALF_OPEN state.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
        half_open_max: int = 1,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.half_open_max = half_open_max

        self._state: CircuitBreakerState = CircuitBreakerState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._half_open_calls: int = 0

    @property
    def state(self) -> CircuitBreakerState:
        """Current state, updated lazily when OPEN timeout has elapsed."""
        if self._state == CircuitBreakerState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.reset_timeout:
                self._state = CircuitBreakerState.HALF_OPEN
                self._half_open_calls = 0
        return self._state

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """
        Execute *func* through the circuit breaker.

        Raises:
            CircuitBreakerOpen: when the circuit is OPEN and no probe is allowed.
        """
        current_state = self.state

        if current_state == CircuitBreakerState.OPEN:
            raise CircuitBreakerOpen(
                f"Circuit breaker is OPEN. Calls blocked for {self.reset_timeout}s."
            )

        if current_state == CircuitBreakerState.HALF_OPEN:
            if self._half_open_calls >= self.half_open_max:
                raise CircuitBreakerOpen(
                    "Circuit breaker is HALF_OPEN and probe limit reached."
                )
            self._half_open_calls += 1

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except CircuitBreakerOpen:
            raise
        except Exception as exc:
            self._on_failure()
            raise exc

    def _on_success(self) -> None:
        """Reset the circuit breaker on a successful call."""
        self._failure_count = 0
        self._half_open_calls = 0
        self._state = CircuitBreakerState.CLOSED

    def _on_failure(self) -> None:
        """Record a failure and potentially open the circuit."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            self._state = CircuitBreakerState.OPEN

    def reset(self) -> None:
        """Manually reset the circuit breaker to CLOSED state."""
        self._state = CircuitBreakerState.CLOSED
        self._failure_count = 0
        self._half_open_calls = 0
        self._last_failure_time = 0.0
