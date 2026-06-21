"""Unit tests for StrategyEngine."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

from option_trading.core.broker import BrokerClient
from option_trading.core.strategy_engine import StrategyEngine
from option_trading.models.strategy import Strategy, StrategyResult, TradeMode
from option_trading.persistence.trade_logger import TradeLogger
from conftest import make_tick


def _make_settings(
    tolerance: int = 15,
    market_close: str = "15:30:00",
    trade_margin: int = 1_000_000,
):
    s = MagicMock()
    s.entry_tolerance_seconds = tolerance
    s.market_close_time = market_close
    s.trade_margin = trade_margin
    return s


def _make_engine(
    strategies: Dict[str, Strategy],
    kite: MagicMock = None,
    settings=None,
) -> StrategyEngine:
    kite = kite or MagicMock()
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"
    broker = BrokerClient(kite)
    broker._kite = kite
    # Stub order placement
    kite.place_order.return_value = "ORDER123"

    trade_logger = TradeLogger()
    settings = settings or _make_settings()

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
    engine.set_ws(ws_mock)
    return engine


def _make_strategy(
    time_str: str = "10:00:00",
    points: float = 10.0,
    trade_type: TradeMode = TradeMode.DUMMY,
    ce_token: int = 1001,
    pe_token: int = 1002,
    tradable: bool = True,
    conditional_on=None,
) -> Strategy:
    return Strategy(
        time_str=time_str,
        index="NSE:NIFTY 50",
        strangle=0,
        points=points,
        trade_type=trade_type,
        conditional_on=conditional_on,
        ce_symbol="NIFTY24620220000CE",
        pe_symbol="NIFTY24620220000PE",
        ce_token=ce_token,
        pe_token=pe_token,
        tradable=tradable,
    )


class TestStrategyEngineEntry:
    def test_entry_recorded_for_dummy_strategy(self):
        strategy = _make_strategy("10:00:00", trade_type=TradeMode.DUMMY)
        strategy.running = True
        strategy.liquidity_exists = True
        engine = _make_engine({"10:00:00": strategy})
        engine._initial_sum["10:00:00"] = None

        ticks = [
            make_tick(1001, 100.0),
            make_tick(1002, 80.0),
        ]

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 0, 5)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        assert engine._initial_sum["10:00:00"] == pytest.approx(180.0)
        records = engine._trade_logger.records
        assert any(r["type"] == "entry" for r in records)

    def test_real_entry_places_sell_orders(self):
        kite = MagicMock()
        kite.TRANSACTION_TYPE_BUY = "BUY"
        kite.TRANSACTION_TYPE_SELL = "SELL"
        kite.place_order.return_value = "ORD001"

        strategy = _make_strategy("10:00:00", trade_type=TradeMode.REAL)
        strategy.running = True
        strategy.liquidity_exists = True
        engine = _make_engine({"10:00:00": strategy}, kite=kite)
        engine._initial_sum["10:00:00"] = None

        ticks = [make_tick(1001, 100.0), make_tick(1002, 80.0)]

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 0, 5)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        # Two SELL orders placed (CE and PE)
        sell_calls = [
            c for c in kite.place_order.call_args_list
            if c.kwargs.get("transaction_type") == "SELL"
            or (c.args and "SELL" in c.args)
        ]
        assert kite.place_order.call_count >= 2


class TestStrategyEngineExit:
    def test_stop_loss_triggers_exit_for_dummy(self):
        strategy = _make_strategy("10:00:00", points=10.0, trade_type=TradeMode.DUMMY)
        strategy.running = True
        strategy.liquidity_exists = True
        engine = _make_engine({"10:00:00": strategy})
        # Set initial sum so any movement triggers stop-loss
        engine._initial_sum["10:00:00"] = 100.0
        engine._current_sum["10:00:00"] = 100.0
        strategy.current_ce_price = 60.0
        strategy.current_pe_price = 50.0  # sum=110 → diff=+10 ≥ points

        ticks = [make_tick(1001, 60.0), make_tick(1002, 50.0)]

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 5, 1)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        assert strategy.completed is True
        records = engine._trade_logger.records
        assert any(r["type"] == "exit" for r in records)

    def test_stop_loss_result_is_profit_when_diff_negative(self):
        strategy = _make_strategy("10:00:00", points=10.0, trade_type=TradeMode.DUMMY)
        strategy.running = True
        strategy.liquidity_exists = True
        engine = _make_engine({"10:00:00": strategy})
        engine._initial_sum["10:00:00"] = 200.0
        strategy.current_ce_price = 90.0
        strategy.current_pe_price = 100.0  # sum=190 → diff=-10 ≥ 10 → profit

        ticks = [make_tick(1001, 90.0), make_tick(1002, 100.0)]

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 5, 1)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        assert strategy.result == StrategyResult.PROFIT


class TestStrategyEngineSkip:
    def test_strategy_skipped_when_time_elapsed(self):
        strategy = _make_strategy("09:00:00")
        engine = _make_engine({"09:00:00": strategy})

        ticks: List[dict] = []
        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        assert strategy.skipped is True
        assert strategy.completed is True

    def test_not_tradable_strategy_marked_avoided(self):
        strategy = _make_strategy("10:00:00", tradable=False)
        engine = _make_engine({"10:00:00": strategy})

        ticks: List[dict] = []
        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 0, 5)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        assert strategy.completed is True


class TestStrategyEngineMarketClose:
    def test_market_close_closes_websocket(self):
        strategy = _make_strategy("10:00:00", trade_type=TradeMode.DUMMY)
        strategy.running = True
        strategy.current_ce_price = 100.0
        strategy.current_pe_price = 80.0
        engine = _make_engine({"10:00:00": strategy})
        engine._initial_sum["10:00:00"] = 180.0

        ticks = [make_tick(1001, 100.0), make_tick(1002, 80.0)]

        settings = _make_settings(market_close="10:00:00")
        engine._settings = settings

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        engine._ws.close.assert_called_once()

    def test_real_strategy_gets_buy_orders_at_market_close(self):
        kite = MagicMock()
        kite.TRANSACTION_TYPE_BUY = "BUY"
        kite.TRANSACTION_TYPE_SELL = "SELL"
        kite.place_order.return_value = "CLOSEORD"

        strategy = _make_strategy("10:00:00", trade_type=TradeMode.REAL)
        strategy.running = True
        strategy.current_ce_price = 100.0
        strategy.current_pe_price = 80.0
        engine = _make_engine({"10:00:00": strategy}, kite=kite)
        engine._initial_sum["10:00:00"] = 180.0
        engine._settings = _make_settings(market_close="10:00:00")

        ticks = [make_tick(1001, 100.0), make_tick(1002, 80.0)]

        with patch("option_trading.core.strategy_engine.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2024, 6, 20, 10, 0, 0)
            mock_dt.strptime = datetime.strptime
            engine.process_ticks(ticks)

        assert kite.place_order.call_count >= 2
