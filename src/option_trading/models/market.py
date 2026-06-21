"""Market data models."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class DepthEntry:
    """Single entry in order-book depth."""

    price: float
    quantity: int
    orders: int = 0


@dataclass
class MarketDepth:
    """Order-book depth (5 levels each side)."""

    buy: List[DepthEntry]
    sell: List[DepthEntry]

    @classmethod
    def from_tick_depth(cls, depth: Dict[str, List[Dict[str, Any]]]) -> "MarketDepth":
        buy = [
            DepthEntry(price=d["price"], quantity=d["quantity"], orders=d.get("orders", 0))
            for d in depth.get("buy", [])
        ]
        sell = [
            DepthEntry(price=d["price"], quantity=d["quantity"], orders=d.get("orders", 0))
            for d in depth.get("sell", [])
        ]
        return cls(buy=buy, sell=sell)


@dataclass
class MarketTick:
    """Normalised market tick from KiteTicker."""

    instrument_token: int
    last_price: float
    depth: Optional[MarketDepth] = None
    volume: int = 0
    oi: int = 0

    @classmethod
    def from_raw(cls, raw: Dict[str, Any]) -> "MarketTick":
        depth = None
        if "depth" in raw:
            depth = MarketDepth.from_tick_depth(raw["depth"])
        return cls(
            instrument_token=raw["instrument_token"],
            last_price=raw.get("last_price", 0.0),
            depth=depth,
            volume=raw.get("volume", 0),
            oi=raw.get("oi", 0),
        )
