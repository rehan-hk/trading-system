"""Unit tests for the circuit breaker."""
from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from option_trading.utils.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpen,
    CircuitBreakerState,
)


def _always_succeed(*args, **kwargs):
    return "ok"


def _always_fail(*args, **kwargs):
    raise ValueError("simulated failure")


class TestCircuitBreakerClosed:
    def test_passes_through_on_success(self):
        cb = CircuitBreaker(failure_threshold=3)
        result = cb.call(_always_succeed)
        assert result == "ok"
        assert cb.state == CircuitBreakerState.CLOSED

    def test_counts_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            with pytest.raises(ValueError):
                cb.call(_always_fail)
        assert cb._failure_count == 2
        assert cb.state == CircuitBreakerState.CLOSED

    def test_passes_function_args(self):
        cb = CircuitBreaker()
        result = cb.call(lambda x, y: x + y, 3, 4)
        assert result == 7

    def test_resets_failure_count_on_success(self):
        cb = CircuitBreaker(failure_threshold=3)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        assert cb._failure_count == 1
        cb.call(_always_succeed)
        assert cb._failure_count == 0


class TestCircuitBreakerOpen:
    def test_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            with pytest.raises(ValueError):
                cb.call(_always_fail)
        assert cb.state == CircuitBreakerState.OPEN

    def test_raises_circuit_breaker_open_when_open(self):
        cb = CircuitBreaker(failure_threshold=1)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        assert cb.state == CircuitBreakerState.OPEN
        with pytest.raises(CircuitBreakerOpen):
            cb.call(_always_succeed)

    def test_does_not_call_func_when_open(self):
        mock_func = MagicMock()
        cb = CircuitBreaker(failure_threshold=1)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        with pytest.raises(CircuitBreakerOpen):
            cb.call(mock_func)
        mock_func.assert_not_called()


class TestCircuitBreakerHalfOpen:
    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        assert cb.state == CircuitBreakerState.OPEN
        time.sleep(0.1)
        assert cb.state == CircuitBreakerState.HALF_OPEN

    def test_closes_on_half_open_success(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        time.sleep(0.1)
        assert cb.state == CircuitBreakerState.HALF_OPEN
        result = cb.call(_always_succeed)
        assert result == "ok"
        assert cb.state == CircuitBreakerState.CLOSED

    def test_opens_again_on_half_open_failure(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        time.sleep(0.1)
        assert cb.state == CircuitBreakerState.HALF_OPEN
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        assert cb.state == CircuitBreakerState.OPEN

    def test_half_open_max_probes_enforced(self):
        cb = CircuitBreaker(failure_threshold=1, reset_timeout=0.05, half_open_max=1)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        time.sleep(0.1)
        # First probe passes (but let's make it fail to stay in OPEN)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        # Should be OPEN again — second call blocked
        with pytest.raises(CircuitBreakerOpen):
            cb.call(_always_succeed)


class TestCircuitBreakerReset:
    def test_manual_reset(self):
        cb = CircuitBreaker(failure_threshold=1)
        with pytest.raises(ValueError):
            cb.call(_always_fail)
        assert cb.state == CircuitBreakerState.OPEN
        cb.reset()
        assert cb.state == CircuitBreakerState.CLOSED
        result = cb.call(_always_succeed)
        assert result == "ok"
