"""
Liquidity evaluation for option instruments.

Checks both the depth quantity and the bid-ask spread to determine whether
a market is liquid enough to trade.
"""
from __future__ import annotations

from typing import Any, Dict, List

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)


def _weighted_avg_price(depth_side: List[Dict[str, Any]]) -> tuple[int, float]:
    """
    Compute total quantity and quantity-weighted average price for one side
    of the order book.

    Args:
        depth_side: List of dicts with 'quantity' and 'price' keys.

    Returns:
        (total_quantity, weighted_average_price)
    """
    total_qty = sum(item["quantity"] for item in depth_side)
    if total_qty == 0:
        return 0, 0.0
    wav = sum(item["quantity"] * item["price"] for item in depth_side) / total_qty
    return total_qty, wav


def check_liquidity(
    tick: Dict[str, Any],
    threshold_quantity: int,
    bid_ask_spread_pct: float = 1.0,
) -> bool:
    """
    Determine whether an option tick has sufficient liquidity to trade.

    Criteria:
    1. Buy-side total quantity > *threshold_quantity*.
    2. Sell-side total quantity > *threshold_quantity*.
    3. Bid-ask spread (as % of mid-price) < *bid_ask_spread_pct*.

    Args:
        tick:               Raw Kite tick dict (must contain 'depth' key).
        threshold_quantity: Minimum required quantity on each side.
        bid_ask_spread_pct: Maximum allowable spread percentage.

    Returns:
        True if the instrument is considered liquid, False otherwise.
    """
    depth = tick.get("depth", {})
    buy_depth: List[Dict[str, Any]] = depth.get("buy", [])
    sell_depth: List[Dict[str, Any]] = depth.get("sell", [])

    buy_qty, buy_wav = _weighted_avg_price(buy_depth)
    sell_qty, sell_wav = _weighted_avg_price(sell_depth)

    if buy_wav > 0:
        spread = ((buy_wav - sell_wav) / buy_wav) * 100.0
    else:
        spread = 100.0  # treat as illiquid if no buy price

    logger.debug(
        "Liquidity check — buy_qty=%d sell_qty=%d buy_wav=%.2f sell_wav=%.2f spread=%.2f%%",
        buy_qty,
        sell_qty,
        buy_wav,
        sell_wav,
        spread,
    )

    return (
        buy_qty > threshold_quantity
        and sell_qty > threshold_quantity
        and spread < bid_ask_spread_pct
    )
