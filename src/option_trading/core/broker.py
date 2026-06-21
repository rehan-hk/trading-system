"""
Broker client — wraps KiteConnect order placement with retry and circuit breaker.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from option_trading.utils.circuit_breaker import CircuitBreaker, CircuitBreakerOpen
from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)

# NSE lot quantities per index
QUANTITIES: dict[str, int] = {
    "NSE:NIFTY BANK": 15,
    "NSE:NIFTY FIN SERVICE": 40,
    "NSE:NIFTY 50": 25,
    "NSE:NIFTY MID SELECT": 50,
}

# Margin required per index (1 lot each side)
MARGIN_REQUIRED: dict[str, int] = {
    "NSE:NIFTY BANK": 220000,
    "NSE:NIFTY FIN SERVICE": 280000,
    "NSE:NIFTY 50": 220000,
    "NSE:NIFTY MID SELECT": 200000,
}


def place_order(
    kite: Any,
    symbol: str,
    quantity: int,
    transaction_type: str,
    order_type: str = "MARKET",
    max_retries: int = 3,
    retry_delay: float = 0.5,
) -> Optional[str]:
    """
    Place a single order with retry logic.

    Args:
        kite:             Authenticated KiteConnect instance.
        symbol:           NSE option trading symbol.
        quantity:         Number of units to trade.
        transaction_type: 'BUY' or 'SELL'.
        order_type:       Kite order type string (default 'MARKET').
        max_retries:      Total attempts before giving up.
        retry_delay:      Seconds between retries.

    Returns:
        Order ID string on success, None on failure.
    """
    for attempt in range(max_retries):
        try:
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=kite.EXCHANGE_NFO,
                tradingsymbol=symbol,
                transaction_type=transaction_type,
                quantity=quantity,
                order_type=order_type,
                product=kite.PRODUCT_MIS,
            )
            logger.info(
                "Order placed — %s %s qty=%d order_id=%s",
                transaction_type,
                symbol,
                quantity,
                order_id,
            )
            return str(order_id)
        except Exception as exc:
            logger.error(
                "Order placement failed (attempt %d/%d) — %s %s: %s",
                attempt + 1,
                max_retries,
                transaction_type,
                symbol,
                exc,
            )
            if attempt < max_retries - 1:
                time.sleep(retry_delay)

    logger.error(
        "Order placement permanently failed after %d retries — %s %s",
        max_retries,
        transaction_type,
        symbol,
    )
    return None


class BrokerClient:
    """
    High-level broker client that wraps order placement with a circuit breaker.

    The circuit breaker opens after *failure_threshold* consecutive placement
    failures, protecting the system from runaway error conditions.
    """

    def __init__(
        self,
        kite: Any,
        failure_threshold: int = 3,
        reset_timeout: float = 30.0,
        max_retries: int = 3,
        retry_delay: float = 0.5,
    ) -> None:
        self._kite = kite
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=failure_threshold,
            reset_timeout=reset_timeout,
        )

    def place_order(
        self,
        symbol: str,
        quantity: int,
        transaction_type: str,
        order_type: str = "MARKET",
    ) -> Optional[str]:
        """
        Place an order through the circuit breaker.

        Returns None (without raising) when the circuit is OPEN so callers
        can treat it identically to a normal failure.
        """
        try:
            return self._circuit_breaker.call(
                place_order,
                self._kite,
                symbol,
                quantity,
                transaction_type,
                order_type,
                self._max_retries,
                self._retry_delay,
            )
        except CircuitBreakerOpen as exc:
            logger.error("Circuit breaker OPEN — order blocked: %s", exc)
            return None
        except Exception as exc:
            logger.error("Unexpected error from broker: %s", exc)
            return None

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._circuit_breaker

    def get_quantity(self, index: str) -> int:
        """Return lot quantity for the given index."""
        return QUANTITIES.get(index, 0)

    def get_margin_required(self, index: str) -> int:
        """Return margin required for one lot of the given index."""
        return MARGIN_REQUIRED.get(index, 0)
