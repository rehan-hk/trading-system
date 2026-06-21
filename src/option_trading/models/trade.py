"""Trade record data model."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Union


class TradeType(str, Enum):
    ENTRY = "entry"
    EXIT = "exit"
    EXIT_AT_CLOSE = "exit at market close"
    SKIPPED = "skipped"
    AVOIDED = "AVOIDED THE STRATEGY"
    NO_LIQUIDITY = "No Liquidity"
    IMPROPER_POSITION = "improper_position"
    FAILED = "FAILED"
    NOT_EXECUTED_CONDITIONAL = "NOT EXECUTED (LOSSY DEPENDENT STRATEGY OR RESULT NOT FOUND)"


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    MARKET_CLOSE = "market_close"
    IMPROPER_FILL = "improper_fill"
    NO_LIQUIDITY = "no_liquidity"
    CONDITIONAL_NOT_MET = "conditional_not_met"


@dataclass
class TradeRecord:
    """A single trade event record."""

    index: str
    strangle: int
    points: float
    datetime: datetime
    trade_mode: str           # "real" or "dummy"
    event_type: str           # one of TradeType values
    sum_price: Union[float, str]
    ce_price: Union[float, str]
    pe_price: Union[float, str]
    price_difference: Union[float, str]
    strategy_time_str: str = ""

    def to_dict(self) -> dict:
        return {
            "index/strategy details": self.index,
            "strangle": self.strangle,
            "point": self.points,
            "datetime": self.datetime.isoformat(),
            "trade_type": self.trade_mode,
            "type": self.event_type,
            "sum_price": self.sum_price,
            "ce_price": self.ce_price,
            "pe_price": self.pe_price,
            "price_difference": self.price_difference,
            "strategy_time_str": self.strategy_time_str,
        }
