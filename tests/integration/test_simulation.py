"""
Integration test — full simulation using synthetic ticks.

Runs the StrategyEngine with synthetic random ticks from 09:15 to 12:00
and verifies that trade records are produced with the expected structure.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Dict, List
from unittest.mock import MagicMock, patch

import pytest

from option_trading.core.broker import BrokerClient
from option_trading.core.strategy_engine import StrategyEngine
from option_trading.models.strategy import Strategy, TradeMode
from option_trading.persistence.trade_logger import TradeLogger
from conftest import make_tick


# ─── Test fixtures ────────────────────────────────────────────────────────────

@pytest.fixture
def sim_settings():
    s = MagicMock()
    s.entry_tolerance_seconds = 15
    s.market_close_time = "12:00:00"  # early close for fast test
    s.trade_margin = 1_000_000
    return s


@pytest.fixture
def sim_strategies() -> Dict[str, Strategy]:
    """Two dummy strategies at known times within the 09:15–12:00 window."""
    return {
        "09:30:00": Strategy(
            time_str="09:30:00",
            index="NSE:NIFTY 50",
            strangle=0,
            points=10.0,
            trade_type=TradeMode.DUMMY,
            conditional_on=None,
            ce_symbol="NIFTY24620220000CE",
            pe_symbol="NIFTY24620220000PE",
            ce_token=2001,
            pe_token=2002,
            tradable=True,
        ),
        "10:00:00": Strategy(
            time_str="10:00:00",
            index="NSE:NIFTY 50",
            strangle=0,
            points=5.0,
            trade_type=TradeMode.DUMMY,
            conditional_on="09:30:00",
            ce_symbol="NIFTY24620220000CE",
            pe_symbol="NIFTY24620220000PE",
            ce_token=2001,
            pe_token=2002,
            tradable=True,
        ),
    }


@pytest.fixture
def sim_engine(sim_strategies, sim_settings) -> StrategyEngine:
    kite = MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    kite.place_order.return_value = "SIM_ORDER"

    broker = BrokerClient(kite)
    trade_logger = TradeLogger()

    engine = StrategyEngine(
        kite=kite,
        instruments_df=MagicMock(),
        strategies=sim_strategies,
        broker=broker,
        trade_logger=trade_logger,
        dynamo_store=None,
        notifier=None,
        settings=sim_settings,
    )

    ws_mock = MagicMock()
    engine.set_ws(ws_mock)
    return engine


# ─── Simulation helpers ───────────────────────────────────────────────────────

def _generate_ticks(
    ce_token: int,
    pe_token: int,
    ce_price: float,
    pe_price: float,
    qty: int = 5000,
) -> List[dict]:
    return [
        make_tick(ce_token, ce_price, buy_qty=qty, sell_qty=qty),
        make_tick(pe_token, pe_price, buy_qty=qty, sell_qty=qty),
    ]


def _run_simulation(engine: StrategyEngine, sim_settings) -> None:
    """Step through synthetic time from 09:15 to 12:00 in 10-second increments."""
    random.seed(42)
    start = datetime(2024, 6, 20, 9, 15, 0)
    end = datetime(2024, 6, 20, 12, 0, 0)
    step = timedelta(seconds=10)

    current = start
    ce_price = 100.0
    pe_price = 80.0

    while current <= end:
        # Random-walk prices
        ce_price = max(1.0, ce_price + random.uniform(-3, 3))
        pe_price = max(1.0, pe_price + random.uniform(-3, 3))

        ticks = _generate_ticks(2001, 2002, ce_price, pe_price)

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = current
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        current += step


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestSimulation:
    def test_simulation_produces_trade_records(self, sim_engine):
        _run_simulation(sim_engine, sim_engine._settings)
        records = sim_engine._trade_logger.records
        assert len(records) > 0

    def test_all_records_have_required_keys(self, sim_engine):
        _run_simulation(sim_engine, sim_engine._settings)
        required_keys = {
            "index/strategy details",
            "strangle",
            "point",
            "datetime",
            "trade_type",
            "type",
            "sum_price",
            "ce_price",
            "pe_price",
            "price_difference",
        }
        for record in sim_engine._trade_logger.records:
            missing = required_keys - record.keys()
            assert not missing, f"Record missing keys: {missing}\nRecord: {record}"

    def test_all_strategies_eventually_complete(self, sim_engine, sim_strategies):
        _run_simulation(sim_engine, sim_engine._settings)
        for time_str, strategy in sim_strategies.items():
            assert strategy.completed is True, (
                f"Strategy {time_str} did not complete. State: running={strategy.running}"
            )

    def test_entry_records_exist(self, sim_engine):
        _run_simulation(sim_engine, sim_engine._settings)
        types = {r["type"] for r in sim_engine._trade_logger.records}
        # At minimum we expect entry or exit or skipped records
        assert types & {"entry", "exit", "skipped", "exit at market close"}

    def test_trade_data_csv_can_be_saved(self, sim_engine, tmp_path):
        _run_simulation(sim_engine, sim_engine._settings)
        csv_path = str(tmp_path / "test_trades.csv")
        ok = sim_engine._trade_logger.save_csv(csv_path)
        assert ok is True

        import pandas as pd
        df = pd.read_csv(csv_path)
        assert len(df) == len(sim_engine._trade_logger.records)

    def test_conditional_strategy_not_executed_when_dependency_lossy(
        self, sim_engine, sim_strategies
    ):
        """
        Force the first strategy to result in a loss and verify the second
        (conditional_on=09:30:00) is not executed.
        """
        # Force first strategy into completed loss state before simulation
        strat_1 = sim_strategies["09:30:00"]
        strat_1.completed = True
        strat_1.running = False
        strat_1.result = None  # Neither profit nor loss → conditional should not execute

        strat_2 = sim_strategies["10:00:00"]

        _run_simulation(sim_engine, sim_engine._settings)

        # The second strategy should not have an entry record (dep not profitable)
        entry_records = [
            r for r in sim_engine._trade_logger.records
            if r.get("strategy_time_str") == "10:00:00" and r["type"] == "entry"
        ]
        assert len(entry_records) == 0, (
            "Conditional strategy should not have entry when dependency not profitable"
        )
