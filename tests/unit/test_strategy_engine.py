"""
Unit tests for StrategyEngine (parallel model) and StrategyWorker.

Each test creates workers directly and calls _process() / _execute() synchronously
(no actual threads) so tests remain fast and deterministic.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from option_trading.core.strategy_engine import (
    MarginTracker,
    StrategyEngine,
    StrategyWorker,
)
from option_trading.models.strategy import Strategy, StrategyResult, TradeMode
from option_trading.models.trade import TradeType
from option_trading.persistence.trade_logger import TradeLogger


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_settings(
    entry_tolerance: int = 15,
    market_close: str = "15:30:00",
    trade_margin: int = 1_000_000,
    trade_mode: str = "dummy",
):
    s = MagicMock()
    s.entry_tolerance_seconds = entry_tolerance
    s.market_close_time = market_close
    s.trade_margin = trade_margin
    s.trade_mode = trade_mode
    return s


def _make_dummy_strategy(
    time_str: str = "10:00:00",
    points: float = 10.0,
    conditional_on: str | None = None,
) -> Strategy:
    return Strategy(
        time_str=time_str,
        index="NSE:NIFTY 50",
        strangle=0,
        points=points,
        trade_type=TradeMode.DUMMY,
        conditional_on=conditional_on,
        ce_token=111,
        pe_token=222,
        ce_symbol="NIFTY24621 21000CE",
        pe_symbol="NIFTY24621 21000PE",
        tradable=True,
        liquidity_exists=True,
    )


def _make_real_strategy(time_str: str = "10:00:00", points: float = 10.0) -> Strategy:
    s = _make_dummy_strategy(time_str, points)
    s.trade_type = TradeMode.REAL
    return s


def _make_worker(
    strategy: Strategy,
    time_str: str = "10:00:00",
    broker: MagicMock | None = None,
    trade_logger: TradeLogger | None = None,
    margin_tracker: MarginTracker | None = None,
    settings=None,
    get_peer_result=None,
) -> StrategyWorker:
    kite = MagicMock()
    kite.TRANSACTION_TYPE_SELL = "SELL"
    kite.TRANSACTION_TYPE_BUY = "BUY"

    return StrategyWorker(
        time_str=time_str,
        strategy=strategy,
        kite=kite,
        instruments_df=MagicMock(),
        broker=broker or MagicMock(),
        trade_logger=trade_logger or TradeLogger(),
        margin_tracker=margin_tracker or MarginTracker(1_000_000),
        settings=settings or _make_settings(),
        get_peer_result=get_peer_result or (lambda _: None),
        ws_lock=threading.Lock(),
    )


def _make_tick(token: int, price: float) -> dict:
    return {
        "instrument_token": token,
        "last_price": price,
        "depth": {
            "buy": [{"quantity": 5000, "price": price - 0.05}] * 5,
            "sell": [{"quantity": 5000, "price": price + 0.05}] * 5,
        },
    }


# ─── MarginTracker ────────────────────────────────────────────────────────────

class TestMarginTracker:
    def test_deduct_reduces_margin(self):
        mt = MarginTracker(1_000_000)
        remaining = mt.deduct(220_000)
        assert remaining == 780_000
        assert mt.available == 780_000

    def test_restore_increases_margin(self):
        mt = MarginTracker(780_000)
        remaining = mt.restore(220_000)
        assert remaining == 1_000_000

    def test_thread_safe_concurrent_deductions(self):
        mt = MarginTracker(1_000_000)
        errors = []

        def deduct():
            try:
                mt.deduct(100_000)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=deduct) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert mt.available == 500_000


# ─── Entry ────────────────────────────────────────────────────────────────────

class TestStrategyEngineEntry:
    def test_entry_recorded_for_dummy_strategy(self):
        strategy = _make_dummy_strategy()
        strategy.running = True
        tl = TradeLogger()
        worker = _make_worker(strategy, trade_logger=tl)

        ticks = [_make_tick(111, 100.0), _make_tick(222, 50.0)]
        worker._execute(ticks, datetime.now())

        assert len(tl.records) == 1
        assert tl.records[0]["type"] == TradeType.ENTRY.value

    def test_real_entry_places_sell_orders(self):
        strategy = _make_real_strategy()
        strategy.running = True
        broker = MagicMock()
        broker.place_order.return_value = "ORDER123"
        broker.get_quantity.return_value = 25
        broker.get_margin_required.return_value = 220_000

        worker = _make_worker(strategy, broker=broker)
        ticks = [_make_tick(111, 100.0), _make_tick(222, 50.0)]
        worker._execute(ticks, datetime.now())

        assert broker.place_order.call_count == 2
        calls = [c.args[2] for c in broker.place_order.call_args_list]
        assert all(side == "SELL" for side in calls)

    def test_partial_fill_ce_only_squares_off(self):
        strategy = _make_real_strategy()
        strategy.running = True
        broker = MagicMock()
        broker.place_order.side_effect = ["CE_ORDER", None, "SQUARE_OFF"]
        broker.get_quantity.return_value = 25
        broker.get_margin_required.return_value = 220_000

        tl = TradeLogger()
        worker = _make_worker(strategy, broker=broker, trade_logger=tl)
        ticks = [_make_tick(111, 100.0), _make_tick(222, 50.0)]
        worker._execute(ticks, datetime.now())

        assert strategy.completed is True
        assert strategy.failed is True
        types = [r["type"] for r in tl.records]
        assert TradeType.IMPROPER_POSITION.value in types


# ─── Exit ─────────────────────────────────────────────────────────────────────

class TestStrategyEngineExit:
    def test_stop_loss_triggers_exit_for_dummy(self):
        strategy = _make_dummy_strategy(points=10.0)
        strategy.running = True
        tl = TradeLogger()
        worker = _make_worker(strategy, trade_logger=tl)

        worker._execute([_make_tick(111, 100.0), _make_tick(222, 50.0)], datetime.now())
        worker._execute([_make_tick(111, 90.0), _make_tick(222, 45.0)], datetime.now())

        types = [r["type"] for r in tl.records]
        assert TradeType.EXIT.value in types
        assert strategy.completed is True

    def test_stop_loss_result_is_profit_when_diff_negative(self):
        strategy = _make_dummy_strategy(points=10.0)
        strategy.running = True
        worker = _make_worker(strategy)

        worker._execute([_make_tick(111, 100.0), _make_tick(222, 50.0)], datetime.now())
        worker._execute([_make_tick(111, 85.0), _make_tick(222, 40.0)], datetime.now())

        assert strategy.result == StrategyResult.PROFIT

    def test_stop_loss_result_is_loss_when_diff_positive(self):
        strategy = _make_dummy_strategy(points=10.0)
        strategy.running = True
        worker = _make_worker(strategy)

        worker._execute([_make_tick(111, 100.0), _make_tick(222, 50.0)], datetime.now())
        worker._execute([_make_tick(111, 115.0), _make_tick(222, 55.0)], datetime.now())

        assert strategy.result == StrategyResult.LOSS


# ─── Skip ─────────────────────────────────────────────────────────────────────

class TestStrategyEngineSkip:
    def test_strategy_skipped_when_time_elapsed(self):
        past_time = (datetime.now() - timedelta(minutes=5)).strftime("%H:%M:%S")
        strategy = _make_dummy_strategy(time_str=past_time)
        tl = TradeLogger()
        worker = _make_worker(strategy, time_str=past_time, trade_logger=tl)

        worker._update_lifecycle(datetime.now())

        assert strategy.skipped is True
        assert strategy.completed is True
        types = [r["type"] for r in tl.records]
        assert TradeType.SKIPPED.value in types

    def test_not_tradable_strategy_marked_avoided(self):
        now_str = datetime.now().strftime("%H:%M:%S")
        strategy = _make_dummy_strategy(time_str=now_str)
        strategy.tradable = False
        tl = TradeLogger()
        worker = _make_worker(strategy, time_str=now_str, trade_logger=tl)

        worker._update_lifecycle(datetime.now())

        assert strategy.completed is True
        types = [r["type"] for r in tl.records]
        assert TradeType.AVOIDED.value in types


# ─── Market close ─────────────────────────────────────────────────────────────

class TestStrategyEngineMarketClose:
    def test_market_close_closes_websocket(self):
        strategy = _make_dummy_strategy()
        strategy.running = True
        strategy.current_ce_price = 100.0
        strategy.current_pe_price = 50.0
        worker = _make_worker(strategy)
        worker._initial_sum = 150.0

        worker._handle_market_close()

        assert strategy.running is False
        assert strategy.completed is True

    def test_real_strategy_gets_buy_orders_at_market_close(self):
        strategy = _make_real_strategy()
        strategy.running = True
        strategy.current_ce_price = 100.0
        strategy.current_pe_price = 50.0
        broker = MagicMock()
        broker.place_order.return_value = "BUY_ORDER"
        broker.get_quantity.return_value = 25
        broker.get_margin_required.return_value = 220_000

        worker = _make_worker(strategy, broker=broker)
        worker._initial_sum = 150.0
        worker._handle_market_close()

        assert broker.place_order.call_count == 2
        calls = [c.args[2] for c in broker.place_order.call_args_list]
        assert all(side == "BUY" for side in calls)


# ─── Conditional strategies ───────────────────────────────────────────────────

class TestConditionalStrategy:
    def test_conditional_not_executed_when_peer_is_loss(self):
        strategy = _make_dummy_strategy(conditional_on="09:00:00")
        strategy.running = True
        tl = TradeLogger()
        worker = _make_worker(
            strategy, trade_logger=tl,
            get_peer_result=lambda _: StrategyResult.LOSS,
        )

        ticks = [_make_tick(111, 100.0), _make_tick(222, 50.0)]
        worker._execute(ticks, datetime.now())

        types = [r["type"] for r in tl.records]
        assert TradeType.NOT_EXECUTED_CONDITIONAL.value in types
        assert strategy.completed is True

    def test_conditional_executes_when_peer_is_profit(self):
        strategy = _make_dummy_strategy(conditional_on="09:00:00")
        strategy.running = True
        tl = TradeLogger()
        worker = _make_worker(
            strategy, trade_logger=tl,
            get_peer_result=lambda _: StrategyResult.PROFIT,
        )

        ticks = [_make_tick(111, 100.0), _make_tick(222, 50.0)]
        worker._execute(ticks, datetime.now())

        types = [r["type"] for r in tl.records]
        assert TradeType.ENTRY.value in types


# ─── Thread safety ────────────────────────────────────────────────────────────

class TestThreadSafety:
    def test_trade_logger_concurrent_writes_lose_no_records(self):
        tl = TradeLogger()
        n_threads, records_each = 10, 50

        def write():
            for i in range(records_each):
                tl.log({"type": "entry", "i": i})

        threads = [threading.Thread(target=write) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(tl.records) == n_threads * records_each

    def test_margin_tracker_no_race_on_concurrent_deductions(self):
        mt = MarginTracker(10 * 220_000)
        threads = [threading.Thread(target=mt.deduct, args=(220_000,)) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert mt.available == 0

    def test_engine_fanout_feeds_all_worker_queues(self):
        from option_trading.core.broker import BrokerClient

        strategies = {
            "10:00:00": _make_dummy_strategy("10:00:00"),
            "11:00:00": _make_dummy_strategy("11:00:00"),
            "12:00:00": _make_dummy_strategy("12:00:00"),
        }
        kite = MagicMock()
        kite.TRANSACTION_TYPE_SELL = "SELL"
        kite.TRANSACTION_TYPE_BUY = "BUY"

        engine = StrategyEngine(
            kite=kite,
            instruments_df=MagicMock(),
            strategies=strategies,
            broker=MagicMock(spec=BrokerClient),
            trade_logger=TradeLogger(),
            dynamo_store=None,
            notifier=None,
            settings=_make_settings(),
        )

        engine.process_ticks([_make_tick(111, 100.0)])

        for worker in engine._workers.values():
            assert not worker._tick_queue.empty()
