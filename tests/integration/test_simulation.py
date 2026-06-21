"""
Integration test — parallel simulation using synthetic ticks.

Starts real StrategyWorker threads, feeds synthetic tick sequences through
the engine fanout, waits for all workers to finish, then asserts on results.

No datetime patching needed — strategies are pre-seeded into RUNNING state
so we test the execution path (entry → exit / market-close) directly.
"""
from __future__ import annotations

import time
import threading
from typing import Dict
from unittest.mock import MagicMock

import pytest

from option_trading.core.broker import BrokerClient
from option_trading.core.strategy_engine import StrategyEngine
from option_trading.models.strategy import Strategy, StrategyResult, TradeMode
from option_trading.models.trade import TradeType
from option_trading.persistence.trade_logger import TradeLogger


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_settings(market_close: str = "15:30:00"):
    s = MagicMock()
    s.entry_tolerance_seconds = 15
    s.market_close_time = market_close
    s.trade_margin = 1_000_000
    s.trade_mode = "dummy"
    return s


def _make_tick(token: int, price: float, qty: int = 5000) -> dict:
    return {
        "instrument_token": token,
        "last_price": price,
        "depth": {
            "buy": [{"quantity": qty, "price": price - 0.05}] * 5,
            "sell": [{"quantity": qty, "price": price + 0.05}] * 5,
        },
    }


def _running_strategy(
    time_str: str,
    points: float = 10.0,
    trade_type: TradeMode = TradeMode.DUMMY,
    conditional_on: str | None = None,
) -> Strategy:
    """Strategy pre-seeded into RUNNING state — bypasses lifecycle checks."""
    return Strategy(
        time_str=time_str,
        index="NSE:NIFTY 50",
        strangle=0,
        points=points,
        trade_type=trade_type,
        conditional_on=conditional_on,
        ce_symbol="NIFTY24621 21000CE",
        pe_symbol="NIFTY24621 21000PE",
        ce_token=2001,
        pe_token=2002,
        tradable=True,
        running=True,          # already started
        liquidity_exists=True, # already confirmed
    )


def _build_engine(strategies: Dict[str, Strategy], trade_logger: TradeLogger, market_close="23:59:59"):
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"

    engine = StrategyEngine(
        kite=kite,
        instruments_df=MagicMock(),
        strategies=strategies,
        broker=MagicMock(spec=BrokerClient),
        trade_logger=trade_logger,
        dynamo_store=None,
        notifier=None,
        settings=_make_settings(market_close),
    )
    ws_mock = MagicMock()
    engine.set_ws(ws_mock)  # starts worker threads
    return engine


def _wait_for_all_done(engine: StrategyEngine, timeout: float = 5.0) -> bool:
    """Block until all workers complete or timeout expires. Returns True if all done."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(w.is_done for w in engine._workers.values()):
            return True
        time.sleep(0.05)
    return False


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestSimulation:

    def test_simulation_produces_trade_records(self):
        """Engine produces at least one trade record per strategy."""
        tl = TradeLogger()
        strategies = {
            "10:00:00": _running_strategy("10:00:00", points=10.0),
        }
        engine = _build_engine(strategies, tl)

        # Entry tick (sum=150), then exit tick (sum=135 → diff=-15 >= 10)
        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 50.0)])
        time.sleep(0.1)
        engine.process_ticks([_make_tick(2001, 88.0), _make_tick(2002, 47.0)])

        _wait_for_all_done(engine)
        assert len(tl.records) > 0

    def test_all_records_have_required_keys(self):
        """Every trade record contains the expected fields."""
        tl = TradeLogger()
        strategies = {"10:00:00": _running_strategy("10:00:00", points=5.0)}
        engine = _build_engine(strategies, tl)

        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 50.0)])
        time.sleep(0.1)
        engine.process_ticks([_make_tick(2001, 92.0), _make_tick(2002, 44.0)])

        _wait_for_all_done(engine)

        required = {
            "index/strategy details", "strangle", "point", "datetime",
            "trade_type", "type", "sum_price", "ce_price", "pe_price",
            "price_difference",
        }
        for record in tl.records:
            missing = required - record.keys()
            assert not missing, f"Record missing keys: {missing}"

    def test_all_strategies_eventually_complete(self):
        """All strategies reach completed=True after ticks cross the exit threshold."""
        tl = TradeLogger()
        strategies = {
            "10:00:00": _running_strategy("10:00:00", points=10.0),
            "11:00:00": _running_strategy("11:00:00", points=8.0),
        }
        engine = _build_engine(strategies, tl)

        # Entry ticks
        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 60.0)])
        time.sleep(0.1)
        # Exit ticks (diff = -15 and -12, both >= respective points)
        engine.process_ticks([_make_tick(2001, 84.0), _make_tick(2002, 51.0)])

        done = _wait_for_all_done(engine)
        assert done, "Not all strategies completed within timeout"
        for time_str, s in strategies.items():
            assert s.completed, f"Strategy {time_str} did not complete"

    def test_entry_records_exist(self):
        """At least one entry record is produced."""
        tl = TradeLogger()
        strategies = {"10:00:00": _running_strategy("10:00:00", points=100.0)}  # high threshold → won't exit
        engine = _build_engine(strategies, tl)

        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 50.0)])
        time.sleep(0.2)

        types = {r["type"] for r in tl.records}
        assert TradeType.ENTRY.value in types

    def test_trade_data_csv_can_be_saved(self, tmp_path):
        """TradeLogger can serialise to CSV after threads write records."""
        tl = TradeLogger()
        strategies = {"10:00:00": _running_strategy("10:00:00", points=10.0)}
        engine = _build_engine(strategies, tl)

        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 50.0)])
        time.sleep(0.1)
        engine.process_ticks([_make_tick(2001, 87.0), _make_tick(2002, 43.0)])

        _wait_for_all_done(engine)

        csv_path = str(tmp_path / "trades.csv")
        ok = tl.save_csv(csv_path)
        assert ok is True

        import pandas as pd
        df = pd.read_csv(csv_path)
        assert len(df) == len(tl.records)

    def test_conditional_strategy_not_executed_when_dependency_lossy(self):
        """
        Strategy B (conditional_on A) must not enter when A resulted in LOSS.
        """
        tl = TradeLogger()

        strat_a = _running_strategy("09:30:00", points=5.0)
        strat_b = _running_strategy("10:00:00", points=5.0, conditional_on="09:30:00")

        strategies = {"09:30:00": strat_a, "10:00:00": strat_b}
        engine = _build_engine(strategies, tl)

        # Force A into a completed LOSS state before any ticks
        strat_a.running = False
        strat_a.completed = True
        strat_a.result = StrategyResult.LOSS

        # Now feed entry tick for B
        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 50.0)])
        time.sleep(0.2)

        entry_records = [
            r for r in tl.records
            if r.get("strategy_time_str") == "10:00:00"
            and r["type"] == TradeType.ENTRY.value
        ]
        assert len(entry_records) == 0, (
            "Conditional strategy B should not enter when A is LOSS"
        )

    def test_two_strategies_exit_in_parallel(self):
        """
        When two strategies hit exit on the same tick batch, both complete
        independently — neither blocks the other's order placement.
        """
        tl = TradeLogger()
        strategies = {
            "10:00:00": _running_strategy("10:00:00", points=10.0),
            "11:00:00": _running_strategy("11:00:00", points=10.0),
        }
        engine = _build_engine(strategies, tl)

        # Entry
        engine.process_ticks([_make_tick(2001, 100.0), _make_tick(2002, 60.0)])
        time.sleep(0.1)

        # Both strategies should exit on this tick (diff = -18 >= 10)
        start = time.monotonic()
        engine.process_ticks([_make_tick(2001, 80.0), _make_tick(2002, 52.0)])

        done = _wait_for_all_done(engine, timeout=3.0)
        elapsed = time.monotonic() - start

        assert done, "Both strategies should complete"

        exit_records = [r for r in tl.records if r["type"] == TradeType.EXIT.value]
        assert len(exit_records) == 2, "Both strategies should have exit records"

        # Verify they ran on different threads (different thread names in logs would
        # confirm, but here we just verify both completed independently)
        for s in strategies.values():
            assert s.result in (StrategyResult.PROFIT, StrategyResult.LOSS)
