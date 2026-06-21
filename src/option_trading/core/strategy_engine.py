"""
Strategy engine — parallel execution model.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │  WebSocket Thread (KiteTicker internal)                         │
    │      └── on_ticks → StrategyEngine.process_ticks(ticks)        │
    │              └── fans out to each StrategyWorker queue          │
    │                  (non-blocking, microseconds)                   │
    └─────────────────────────────────────────────────────────────────┘
                         │            │            │
              ┌──────────┘  ┌─────────┘  ┌────────┘
              ▼             ▼            ▼
    ┌──────────────┐ ┌──────────────┐ ┌──────────────┐
    │ Worker-1     │ │ Worker-2     │ │ Worker-3     │
    │ (own thread) │ │ (own thread) │ │ (own thread) │
    │              │ │              │ │              │
    │ tick_queue   │ │ tick_queue   │ │ tick_queue   │
    │ lifecycle    │ │ lifecycle    │ │ lifecycle    │
    │ entry/exit   │ │ entry/exit   │ │ entry/exit   │
    │ place_order  │ │ place_order  │ │ place_order  │
    │ (blocks here │ │ (blocks here │ │ (blocks here │
    │  not on WS)  │ │  not on WS)  │ │  not on WS)  │
    └──────────────┘ └──────────────┘ └──────────────┘

Key properties:
  - Tick fanout is O(n) dict lookups — never blocks the WebSocket thread.
  - Each worker's order placement blocks only that worker's thread.
  - Two strategies hitting exit simultaneously place orders in true parallel.
  - MarginTracker is the only shared mutable state; protected by a Lock.
  - TradeLogger appends are protected by a Lock.
"""
from __future__ import annotations

import queue
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from option_trading.core.broker import BrokerClient
from option_trading.models.strategy import Strategy, StrategyResult, TradeMode
from option_trading.models.trade import TradeRecord, TradeType
from option_trading.persistence.dynamo_state import DynamoStateStore
from option_trading.persistence.trade_logger import TradeLogger
from option_trading.services.instruments import get_tokens
from option_trading.services.liquidity import check_liquidity
from option_trading.utils.logging_setup import get_logger, set_correlation_id
from option_trading.utils.time_utils import add_seconds_to_time, subtract_seconds_from_time

logger = get_logger(__name__)

LIQUIDITY_THRESHOLDS: Dict[str, int] = {
    "NSE:NIFTY FIN SERVICE": 3600,
    "NSE:NIFTY BANK": 3000,
    "NSE:NIFTY 50": 1000,
    "NSE:NIFTY MID SELECT": 0,
}


# ─── Shared margin tracker ────────────────────────────────────────────────────

class MarginTracker:
    """Thread-safe margin budget shared across all strategy workers."""

    def __init__(self, initial: int) -> None:
        self._margin = initial
        self._lock = threading.Lock()

    def deduct(self, amount: int) -> int:
        with self._lock:
            self._margin -= amount
            return self._margin

    def restore(self, amount: int) -> int:
        with self._lock:
            self._margin += amount
            return self._margin

    @property
    def available(self) -> int:
        return self._margin


# ─── Per-strategy worker ──────────────────────────────────────────────────────

class StrategyWorker:
    """
    Owns a single strategy and runs it on a dedicated daemon thread.

    The WebSocket thread feeds tick batches into ``feed_ticks()`` (non-blocking).
    The worker thread drains its own queue and performs all blocking work
    (order placement, token refresh) independently of every other worker.

    Args:
        time_str:        Strategy entry time key e.g. "10:21:00".
        strategy:        Strategy dataclass instance.
        kite:            Authenticated KiteConnect instance.
        instruments_df:  NFO instruments DataFrame.
        broker:          BrokerClient with circuit breaker.
        trade_logger:    Shared TradeLogger (thread-safe).
        margin_tracker:  Shared MarginTracker (thread-safe).
        settings:        Settings instance.
        get_peer_result: Callable(time_str) -> StrategyResult | None.
                         Used for conditional strategy resolution.
        ws_lock:         Shared lock for WebSocket subscription calls.
    """

    def __init__(
        self,
        time_str: str,
        strategy: Strategy,
        kite: Any,
        instruments_df: Any,
        broker: BrokerClient,
        trade_logger: TradeLogger,
        margin_tracker: MarginTracker,
        settings: Any,
        get_peer_result: Callable[[str], Optional[StrategyResult]],
        ws_lock: threading.Lock,
    ) -> None:
        self._time_str = time_str
        self._strategy = strategy
        self._kite = kite
        self._instruments_df = instruments_df
        self._broker = broker
        self._trade_logger = trade_logger
        self._margin_tracker = margin_tracker
        self._settings = settings
        self._get_peer_result = get_peer_result
        self._ws_lock = ws_lock

        # Bounded queue — drops oldest ticks if worker falls behind
        self._tick_queue: queue.Queue = queue.Queue(maxsize=200)
        self._ws: Optional[Any] = None

        # Per-strategy price state (only touched by this thread)
        self._initial_sum: Optional[float] = None
        self._current_sum: Optional[float] = None
        self._price_diff: float = 0.0

        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"strategy-{time_str}",
        )

    # ── Public API (called from other threads) ────────────────────────────────

    def start(self) -> None:
        self._thread.start()
        logger.info(
            "Worker thread started — strategy=%s thread=%s",
            self._time_str,
            self._thread.name,
        )

    def set_ws(self, ws: Any) -> None:
        self._ws = ws

    def feed_ticks(self, ticks: List[Dict[str, Any]]) -> None:
        """
        Called from the WebSocket thread. Non-blocking.
        Drops the batch if the worker is behind (queue full).
        """
        try:
            self._tick_queue.put_nowait(ticks)
        except queue.Full:
            logger.warning(
                "Tick queue full for strategy %s — dropping batch", self._time_str
            )

    @property
    def is_done(self) -> bool:
        return self._strategy.completed

    @property
    def strategy(self) -> Strategy:
        return self._strategy

    # ── Worker thread main loop ───────────────────────────────────────────────

    def _run(self) -> None:
        logger.info(
            "Strategy %s worker running on thread [%s]",
            self._time_str,
            threading.current_thread().name,
        )
        market_close = datetime.strptime(
            self._settings.market_close_time, "%H:%M:%S"
        ).time()

        while not self._strategy.completed:
            try:
                ticks = self._tick_queue.get(timeout=1.0)
            except queue.Empty:
                # Even with no ticks, check market close
                if datetime.now().time() >= market_close and self._strategy.running:
                    self._handle_market_close()
                continue

            try:
                self._process(ticks)
            except Exception as exc:
                logger.error(
                    "Unhandled error in strategy %s: %s",
                    self._time_str,
                    exc,
                    exc_info=True,
                )
            finally:
                self._tick_queue.task_done()

        logger.info(
            "Strategy %s finished — result=%s",
            self._time_str,
            self._strategy.result,
        )

    def _process(self, ticks: List[Dict[str, Any]]) -> None:
        current_time = datetime.now()
        market_close = datetime.strptime(
            self._settings.market_close_time, "%H:%M:%S"
        ).time()

        if current_time.time() >= market_close:
            self._handle_market_close()
            return

        self._update_lifecycle(current_time)

        if self._strategy.running:
            self._execute(ticks, current_time)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _update_lifecycle(self, current_time: datetime) -> None:
        strategy_time = datetime.strptime(self._time_str, "%H:%M:%S").time()
        tolerance = self._settings.entry_tolerance_seconds
        tolerance_before = subtract_seconds_from_time(strategy_time, tolerance)
        tolerance_after = add_seconds_to_time(strategy_time, tolerance)

        # Time elapsed — skip
        if (
            current_time.time() > tolerance_after
            and not self._strategy.running
            and not self._strategy.completed
        ):
            logger.info("Skipping strategy %s — time elapsed.", self._time_str)
            self._strategy.completed = True
            self._strategy.skipped = True
            self._log_trade(TradeType.SKIPPED)
            return

        in_window = tolerance_before <= current_time.time() <= tolerance_after
        if not in_window or self._strategy.running or self._strategy.completed:
            return

        # DTE check
        if not self._strategy.tradable:
            logger.info(
                "Avoiding strategy %s — tradable=False (DTE not 0).", self._time_str
            )
            self._strategy.running = False
            self._strategy.completed = True
            self._strategy.skipped = True
            self._log_trade(TradeType.AVOIDED)
            return

        # Start
        logger.info(
            "Starting strategy %s on thread [%s] — index=%s",
            self._time_str,
            threading.current_thread().name,
            self._strategy.index,
        )
        self._strategy.running = True
        self._initial_sum = None
        self._strategy.current_ce_price = None
        self._strategy.current_pe_price = None

        # Refresh tokens with current spot price
        ce_sym, pe_sym, ce_tok, pe_tok = get_tokens(
            self._kite, self._instruments_df, self._strategy.index, self._strategy.strangle
        )
        self._strategy.ce_symbol = ce_sym
        self._strategy.pe_symbol = pe_sym
        self._strategy.ce_token = ce_tok
        self._strategy.pe_token = pe_tok

        # Update WebSocket subscription (shared resource — use lock)
        if ce_tok and pe_tok and self._ws is not None:
            with self._ws_lock:
                current_subs = getattr(self._ws, "_subscribed_tokens", set())
                current_subs.add(int(ce_tok))
                current_subs.add(int(pe_tok))
                token_list = list(current_subs)
                self._ws.subscribe(token_list)
                self._ws.set_mode(self._ws.MODE_FULL, token_list)
                logger.info(
                    "Strategy %s subscribed tokens: %s", self._time_str, token_list
                )

    # ── Execution ─────────────────────────────────────────────────────────────

    def _execute(self, ticks: List[Dict[str, Any]], current_time: datetime) -> None:
        # Update prices from this tick batch
        for tick in ticks:
            if tick["instrument_token"] == self._strategy.ce_token:
                self._strategy.current_ce_price = tick["last_price"]
            if tick["instrument_token"] == self._strategy.pe_token:
                self._strategy.current_pe_price = tick["last_price"]

            # Liquidity check — only once, on first tick
            if self._strategy.liquidity_exists is None:
                threshold = LIQUIDITY_THRESHOLDS.get(self._strategy.index, 0)
                if check_liquidity(tick, threshold):
                    self._strategy.liquidity_exists = True
                    logger.info("Liquidity confirmed for strategy %s", self._time_str)
                else:
                    logger.warning(
                        "No liquidity for strategy %s — skipping", self._time_str
                    )
                    self._strategy.liquidity_exists = False
                    self._strategy.running = False
                    self._strategy.completed = True
                    self._strategy.skipped = True
                    self._strategy.current_ce_price = None
                    self._strategy.current_pe_price = None
                    self._initial_sum = None
                    self._log_trade(TradeType.NO_LIQUIDITY)
                    return

        ce = self._strategy.current_ce_price
        pe = self._strategy.current_pe_price

        if ce is None or pe is None:
            return
        if not self._strategy.running or self._strategy.completed:
            return
        if self._strategy.liquidity_exists is not True:
            return

        self._current_sum = ce + pe

        # Entry
        if self._initial_sum is None:
            self._initial_sum = self._current_sum
            logger.info(
                "Entry — strategy=%s thread=[%s] sum=%.2f ce=%.2f pe=%.2f",
                self._time_str,
                threading.current_thread().name,
                self._initial_sum,
                ce,
                pe,
            )
            self._handle_entry(ce, pe)
            return

        # Stop-loss / take-profit check
        if self._strategy.running and not self._strategy.completed:
            self._price_diff = self._current_sum - self._initial_sum
            second = current_time.second
            stop_loss = (
                self._strategy.points
                if (self._price_diff < 0 or second == 0)
                else 1.0 * self._strategy.points
            )

            logger.debug(
                "Stop-loss check — strategy=%s diff=%.2f stop_loss=%.2f",
                self._time_str,
                self._price_diff,
                stop_loss,
            )

            if abs(self._price_diff) >= stop_loss:
                self._handle_exit(ce, pe)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _handle_entry(self, ce: float, pe: float) -> None:
        if self._strategy.trade_type == TradeMode.DUMMY:
            # Conditional check for dummy
            if self._strategy.conditional_on is not None:
                peer_result = self._get_peer_result(self._strategy.conditional_on)
                if peer_result != StrategyResult.PROFIT:
                    logger.info(
                        "Dummy strategy %s not executed — conditional not profitable",
                        self._time_str,
                    )
                    self._log_trade(
                        TradeType.NOT_EXECUTED_CONDITIONAL, ce=ce, pe=pe,
                        sum_price=self._current_sum,
                    )
                    self._close()
                    return
            self._log_trade(TradeType.ENTRY, ce=ce, pe=pe, sum_price=self._initial_sum)
            return

        # Real trade — conditional check
        if self._strategy.conditional_on is not None:
            peer_result = self._get_peer_result(self._strategy.conditional_on)
            if peer_result != StrategyResult.PROFIT:
                logger.info(
                    "Real strategy %s not executed — conditional not profitable",
                    self._time_str,
                )
                self._log_trade(
                    TradeType.NOT_EXECUTED_CONDITIONAL, ce=ce, pe=pe,
                    sum_price=self._current_sum,
                )
                self._close()
                return

        # Place SELL orders (blocking — but only this thread blocks)
        qty = self._broker.get_quantity(self._strategy.index)
        order_ce = self._broker.place_order(
            self._strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_SELL
        )
        order_pe = self._broker.place_order(
            self._strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_SELL
        )

        if order_ce is not None and order_pe is not None:
            margin = self._margin_tracker.deduct(
                self._broker.get_margin_required(self._strategy.index)
            )
            logger.info(
                "Entry orders placed — strategy=%s ce=%s pe=%s margin_remaining=%d",
                self._time_str, order_ce, order_pe, margin,
            )
            self._log_trade(TradeType.ENTRY, ce=ce, pe=pe, sum_price=self._initial_sum)

        elif order_ce is None and order_pe is not None:
            logger.warning("Partial fill (only PE) — squaring off PE for %s", self._time_str)
            self._broker.place_order(
                self._strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
            )
            self._log_trade(TradeType.IMPROPER_POSITION, ce=ce, pe=pe, sum_price=self._current_sum)
            self._close(failed=True)

        elif order_ce is not None and order_pe is None:
            logger.warning("Partial fill (only CE) — squaring off CE for %s", self._time_str)
            self._broker.place_order(
                self._strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
            )
            self._log_trade(TradeType.IMPROPER_POSITION, ce=ce, pe=pe, sum_price=self._current_sum)
            self._close(failed=True)

        else:
            logger.error("Both entry orders failed for strategy %s", self._time_str)
            self._log_trade(TradeType.FAILED, ce=ce, pe=pe, sum_price=self._current_sum)
            self._close(failed=True)

    # ── Exit ──────────────────────────────────────────────────────────────────

    def _handle_exit(self, ce: float, pe: float) -> None:
        result = StrategyResult.PROFIT if self._price_diff < 0 else StrategyResult.LOSS

        if self._strategy.trade_type == TradeMode.DUMMY:
            self._log_trade(
                TradeType.EXIT, ce=ce, pe=pe,
                sum_price=self._current_sum, price_diff=self._price_diff,
            )
            self._strategy.running = False
            self._strategy.completed = True
            self._strategy.result = result
            self._strategy.current_ce_price = None
            self._strategy.current_pe_price = None
            self._initial_sum = None
            logger.info(
                "Dummy exit — strategy=%s result=%s diff=%.2f",
                self._time_str, result.value, self._price_diff,
            )
            return

        # Real exit — place BUY orders (blocking — only this thread)
        qty = self._broker.get_quantity(self._strategy.index)
        order_ce = self._broker.place_order(
            self._strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
        )
        order_pe = self._broker.place_order(
            self._strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
        )
        margin = self._margin_tracker.restore(
            self._broker.get_margin_required(self._strategy.index)
        )
        logger.info(
            "Exit orders placed — strategy=%s ce=%s pe=%s result=%s margin_restored=%d",
            self._time_str, order_ce, order_pe, result.value, margin,
        )
        self._log_trade(
            TradeType.EXIT, ce=ce, pe=pe,
            sum_price=self._current_sum, price_diff=self._price_diff,
        )
        self._strategy.running = False
        self._strategy.completed = True
        self._strategy.result = result
        self._strategy.current_ce_price = None
        self._strategy.current_pe_price = None
        self._initial_sum = None

    # ── Market close ──────────────────────────────────────────────────────────

    def _handle_market_close(self) -> None:
        ce = self._strategy.current_ce_price
        pe = self._strategy.current_pe_price
        current_sum = (ce + pe) if (ce is not None and pe is not None) else None
        diff = (
            (current_sum - self._initial_sum)
            if (current_sum is not None and self._initial_sum is not None)
            else None
        )
        result = (
            StrategyResult.PROFIT if (diff is not None and diff < 0)
            else StrategyResult.LOSS
        )

        if self._strategy.trade_type == TradeMode.REAL:
            qty = self._broker.get_quantity(self._strategy.index)
            self._broker.place_order(
                self._strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
            )
            self._broker.place_order(
                self._strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
            )
            self._margin_tracker.restore(
                self._broker.get_margin_required(self._strategy.index)
            )

        self._strategy.running = False
        self._strategy.completed = True
        self._strategy.result = result

        self._log_trade(
            TradeType.EXIT_AT_CLOSE,
            ce=ce if ce is not None else "-",
            pe=pe if pe is not None else "-",
            sum_price=current_sum if current_sum is not None else "N/A",
            price_diff=diff if diff is not None else "N/A",
        )
        logger.info(
            "Market close square-off — strategy=%s result=%s", self._time_str, result.value
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _close(self, failed: bool = False) -> None:
        self._strategy.running = False
        self._strategy.completed = True
        self._strategy.failed = failed
        self._strategy.current_ce_price = None
        self._strategy.current_pe_price = None
        self._initial_sum = None

    def _log_trade(
        self,
        event_type: TradeType,
        ce: Any = "-",
        pe: Any = "-",
        sum_price: Any = "-",
        price_diff: Any = "-",
    ) -> None:
        record = TradeRecord(
            index=self._strategy.index,
            strangle=self._strategy.strangle,
            points=self._strategy.points,
            datetime=datetime.now(),
            trade_mode=self._strategy.trade_type.value,
            event_type=event_type.value,
            sum_price=sum_price,
            ce_price=ce,
            pe_price=pe,
            price_difference=price_diff,
            strategy_time_str=self._time_str,
        )
        self._trade_logger.log(record.to_dict())


# ─── Engine (tick fanout coordinator) ────────────────────────────────────────

class StrategyEngine:
    """
    Receives tick batches from the WebSocket thread and fans them out
    to per-strategy worker queues in O(n) non-blocking operations.

    All blocking work (order placement, token refresh) happens inside
    individual StrategyWorker threads.
    """

    def __init__(
        self,
        kite: Any,
        instruments_df: Any,
        strategies: Dict[str, Strategy],
        broker: BrokerClient,
        trade_logger: TradeLogger,
        dynamo_store: Optional[DynamoStateStore],
        notifier: Optional[Any],
        settings: Any,
    ) -> None:
        self._strategies = strategies
        self._ws: Optional[Any] = None
        self._ws_lock = threading.Lock()

        margin_tracker = MarginTracker(settings.trade_margin)

        self._workers: Dict[str, StrategyWorker] = {
            time_str: StrategyWorker(
                time_str=time_str,
                strategy=strategy,
                kite=kite,
                instruments_df=instruments_df,
                broker=broker,
                trade_logger=trade_logger,
                margin_tracker=margin_tracker,
                settings=settings,
                get_peer_result=self._get_strategy_result,
                ws_lock=self._ws_lock,
            )
            for time_str, strategy in strategies.items()
        }

        logger.info(
            "StrategyEngine initialised — %d workers, %d threads will run in parallel.",
            len(self._workers),
            len(self._workers),
        )

    def set_ws(self, ws: Any) -> None:
        """
        Called by WebSocketManager after the KiteTicker is created.
        Distributes the ws reference to all workers and starts their threads.
        """
        self._ws = ws
        for worker in self._workers.values():
            worker.set_ws(ws)
        for worker in self._workers.values():
            worker.start()

    def process_ticks(self, ticks: List[Dict[str, Any]]) -> None:
        """
        Called from the WebSocket thread on every tick batch.
        Just fans out to worker queues — must return in microseconds.
        """
        set_correlation_id()

        for worker in self._workers.values():
            worker.feed_ticks(ticks)

        # Close WebSocket once all strategies are done
        if all(w.is_done for w in self._workers.values()):
            logger.info("All strategies completed — closing WebSocket.")
            if self._ws is not None:
                self._ws.close()

    @property
    def strategies(self) -> Dict[str, Strategy]:
        return self._strategies

    def _get_strategy_result(self, time_str: str) -> Optional[StrategyResult]:
        """Used by workers to check peer strategy results for conditional logic."""
        worker = self._workers.get(time_str)
        return worker.strategy.result if worker else None
