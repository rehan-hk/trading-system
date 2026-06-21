"""Strategy data model."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class StrategyResult(str, Enum):
    PROFIT = "profit"
    LOSS = "loss"


class TradeMode(str, Enum):
    REAL = "real"
    DUMMY = "dummy"


@dataclass
class Strategy:
    """Represents a single option-strangle strategy instance."""

    # Identity
    time_str: str                          # e.g. "11:24:40"
    index: str                             # NSE instrument key e.g. "NSE:NIFTY 50"
    strangle: int                          # distance from ATM in points
    points: float                          # stop-loss / take-profit threshold
    trade_type: TradeMode                  # real or dummy
    conditional_on: Optional[str] = None  # time_str of a prerequisite strategy

    # Token resolution (filled in at run-time)
    ce_symbol: Optional[str] = None
    pe_symbol: Optional[str] = None
    ce_token: Optional[int] = None
    pe_token: Optional[int] = None

    # Live state
    running: bool = False
    completed: bool = False
    skipped: bool = False
    failed: bool = False
    tradable: Optional[bool] = None        # None = not yet determined
    liquidity_exists: Optional[bool] = None

    # Prices during execution
    current_ce_price: Optional[float] = None
    current_pe_price: Optional[float] = None

    # Outcome
    result: Optional[StrategyResult] = None


@dataclass
class StrategyState:
    """Serialisable snapshot of strategy state for DynamoDB persistence."""

    time_str: str
    index: str
    strangle: int
    points: float
    trade_type: str
    conditional_on: Optional[str]
    ce_symbol: Optional[str]
    pe_symbol: Optional[str]
    ce_token: Optional[int]
    pe_token: Optional[int]
    running: bool
    completed: bool
    skipped: bool
    failed: bool
    tradable: Optional[bool]
    liquidity_exists: Optional[bool]
    current_ce_price: Optional[float]
    current_pe_price: Optional[float]
    result: Optional[str]

    @classmethod
    def from_strategy(cls, s: Strategy) -> "StrategyState":
        return cls(
            time_str=s.time_str,
            index=s.index,
            strangle=s.strangle,
            points=s.points,
            trade_type=s.trade_type.value,
            conditional_on=s.conditional_on,
            ce_symbol=s.ce_symbol,
            pe_symbol=s.pe_symbol,
            ce_token=s.ce_token,
            pe_token=s.pe_token,
            running=s.running,
            completed=s.completed,
            skipped=s.skipped,
            failed=s.failed,
            tradable=s.tradable,
            liquidity_exists=s.liquidity_exists,
            current_ce_price=s.current_ce_price,
            current_pe_price=s.current_pe_price,
            result=s.result.value if s.result else None,
        )

    def to_dict(self) -> dict:
        return {
            "time_str": self.time_str,
            "index": self.index,
            "strangle": self.strangle,
            "points": self.points,
            "trade_type": self.trade_type,
            "conditional_on": self.conditional_on,
            "ce_symbol": self.ce_symbol,
            "pe_symbol": self.pe_symbol,
            "ce_token": self.ce_token,
            "pe_token": self.pe_token,
            "running": self.running,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "tradable": self.tradable,
            "liquidity_exists": self.liquidity_exists,
            "current_ce_price": self.current_ce_price,
            "current_pe_price": self.current_pe_price,
            "result": self.result,
        }
