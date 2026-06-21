#!/usr/bin/env python3
"""
Standalone simulation script.

Imports from the option_trading package and runs a full trading simulation
with synthetic random ticks from 09:15 to 12:00, printing a summary of
trade results at the end.

Usage:
    pip install -e .
    python scripts/simulate.py
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from typing import Dict, List
from unittest.mock import MagicMock

# Ensure the package is importable when running from the repo root
sys.path.insert(0, "src")

from option_trading.core.broker import BrokerClient
from option_trading.core.strategy_engine import StrategyEngine
from option_trading.models.strategy import Strategy, StrategyResult, TradeMode
from option_trading.persistence.trade_logger import TradeLogger
from option_trading.utils.logging_setup import configure_logging, get_logger

configure_logging(level="INFO")
logger = get_logger("simulate")


# ─── Configuration ────────────────────────────────────────────────────────────

RANDOM_SEED = 42
START_TIME = datetime(2024, 6, 20, 9, 15, 0)
END_TIME = datetime(2024, 6, 20, 12, 0, 0)
TICK_INTERVAL_SECONDS = 10   # simulate one tick every 10 seconds

CE_TOKEN = 2001
PE_TOKEN = 2002

STRATEGIES: Dict[str, dict] = {
    "09:30:00": {
        "index": "NSE:NIFTY 50",
        "strangle": 0,
        "points": 10.0,
        "trade_type": TradeMode.DUMMY,
        "conditional_on": None,
    },
    "10:00:00": {
        "index": "NSE:NIFTY 50",
        "strangle": 0,
        "points": 5.0,
        "trade_type": TradeMode.DUMMY,
        "conditional_on": "09:30:00",
    },
    "10:30:00": {
        "index": "NSE:NIFTY 50",
        "strangle": 0,
        "points": 8.0,
        "trade_type": TradeMode.DUMMY,
        "conditional_on": None,
    },
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_settings(
    tolerance: int = 15,
    market_close: str = "12:00:00",
    trade_margin: int = 1_000_000,
) -> MagicMock:
    s = MagicMock()
    s.entry_tolerance_seconds = tolerance
    s.market_close_time = market_close
    s.trade_margin = trade_margin
    return s


def _make_tick(
    token: int,
    price: float,
    buy_qty: int = 5000,
    sell_qty: int = 5000,
) -> dict:
    buy_price = price * 1.005
    sell_price = price * 0.995
    return {
        "instrument_token": token,
        "last_price": price,
        "depth": {
            "buy": [
                {"quantity": buy_qty // 5, "price": buy_price, "orders": 1}
                for _ in range(5)
            ],
            "sell": [
                {"quantity": sell_qty // 5, "price": sell_price, "orders": 1}
                for _ in range(5)
            ],
        },
        "volume": 100000,
        "oi": 50000,
    }


# ─── Main simulation ──────────────────────────────────────────────────────────

def main() -> None:
    random.seed(RANDOM_SEED)
    print("\n" + "=" * 60)
    print("  Option Trading Engine — Simulation Mode")
    print(f"  Simulating {START_TIME.strftime('%H:%M')} → {END_TIME.strftime('%H:%M')}")
    print("=" * 60 + "\n")

    # Build Strategy objects
    strategies: Dict[str, Strategy] = {}
    for time_str, cfg in STRATEGIES.items():
        strategies[time_str] = Strategy(
            time_str=time_str,
            index=cfg["index"],
            strangle=cfg["strangle"],
            points=cfg["points"],
            trade_type=cfg["trade_type"],
            conditional_on=cfg["conditional_on"],
            ce_symbol="NIFTY24620220000CE",
            pe_symbol="NIFTY24620220000PE",
            ce_token=CE_TOKEN,
            pe_token=PE_TOKEN,
            tradable=True,
        )

    # Build engine
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    kite.place_order.return_value = "SIM_ORDER"

    broker = BrokerClient(kite)
    trade_logger = TradeLogger()
    settings = _make_settings()

    engine = StrategyEngine(
        kite=kite,
        instruments_df=MagicMock(),
        strategies=strategies,
        broker=broker,
        trade_logger=trade_logger,
        dynamo_store=None,
        notifier=None,
        settings=settings,
    )

    ws_mock = MagicMock()
    ws_mock.MODE_FULL = "full"
    engine.set_ws(ws_mock)

    # Run simulation
    ce_price = 100.0
    pe_price = 80.0
    current = START_TIME
    step = timedelta(seconds=TICK_INTERVAL_SECONDS)
    tick_count = 0

    while current <= END_TIME:
        ce_price = max(1.0, ce_price + random.gauss(0, 2))
        pe_price = max(1.0, pe_price + random.gauss(0, 2))

        ticks = [
            _make_tick(CE_TOKEN, round(ce_price, 2)),
            _make_tick(PE_TOKEN, round(pe_price, 2)),
        ]

        # Monkey-patch datetime.now() inside the engine for the current sim time
        import option_trading.core.strategy_engine as _eng_mod
        _orig_datetime = _eng_mod.datetime

        class _FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return current
            @classmethod
            def strptime(cls, *args, **kwargs):
                return _orig_datetime.strptime(*args, **kwargs)
            @classmethod
            def combine(cls, *args, **kwargs):
                return _orig_datetime.combine(*args, **kwargs)

        _eng_mod.datetime = _FakeDatetime
        try:
            engine.process_ticks(ticks)
        finally:
            _eng_mod.datetime = _orig_datetime

        tick_count += 1
        current += step

    # ── Print results ──────────────────────────────────────────────────────────
    records = trade_logger.records
    print(f"\nTotal tick batches processed: {tick_count}")
    print(f"Total trade events recorded:  {len(records)}\n")

    if records:
        print("-" * 60)
        print(f"{'Time':<12} {'Type':<30} {'Sum':>8} {'Diff':>8}")
        print("-" * 60)
        for r in records:
            t = r.get("datetime", "")
            if hasattr(t, "strftime"):
                t = t.strftime("%H:%M:%S")
            t = str(t)[:8]
            event_type = str(r.get("type", ""))[:29]
            sum_p = r.get("sum_price", "-")
            diff = r.get("price_difference", "-")
            sum_str = f"{sum_p:.2f}" if isinstance(sum_p, (int, float)) else str(sum_p)
            diff_str = f"{diff:.2f}" if isinstance(diff, (int, float)) else str(diff)
            print(f"{t:<12} {event_type:<30} {sum_str:>8} {diff_str:>8}")

    print("\n" + "-" * 60)
    print("Strategy outcomes:")
    for time_str, strategy in strategies.items():
        status = "completed" if strategy.completed else "running"
        result = strategy.result.value if strategy.result else "n/a"
        skipped = " (skipped)" if strategy.skipped else ""
        print(
            f"  {time_str}: {status}{skipped} | result={result} | "
            f"tradable={strategy.tradable}"
        )

    print("\nSimulation complete.\n")


if __name__ == "__main__":
    main()
