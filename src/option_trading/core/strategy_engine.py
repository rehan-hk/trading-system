"""
Strategy engine — the heart of the trading system.

Processes each batch of market ticks and drives the strategy lifecycle:
    PENDING → RUNNING → (COMPLETED | SKIPPED | FAILED)

All side-effects (order placement, logging, persistence) are injected
via dependencies so the engine is fully testable without live connections.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from option_trading.core.broker import BrokerClient
from option_trading.models.strategy import Strategy, StrategyResult, TradeMode
from option_trading.models.trade import TradeRecord, TradeType
from option_trading.persistence.dynamo_state import DynamoStateStore
from option_trading.persistence.trade_logger import TradeLogger
from option_trading.services.instruments import get_tokens
from option_trading.services.liquidity import check_liquidity
from option_trading.services.notifications import send_alert
from option_trading.utils.logging_setup import get_logger, set_correlation_id
from option_trading.utils.time_utils import (
    add_seconds_to_time,
    subtract_seconds_from_time,
)

logger = get_logger(__name__)

# Liquidity thresholds per index
LIQUIDITY_THRESHOLDS: Dict[str, int] = {
    "NSE:NIFTY FIN SERVICE": 3600,
    "NSE:NIFTY BANK": 3000,
    "NSE:NIFTY 50": 1000,
    "NSE:NIFTY MID SELECT": 0,
}


class StrategyEngine:
    """
    Drives option-strangle strategy execution based on market tick data.

    Args:
        kite:           Authenticated KiteConnect instance.
        instruments_df: NFO instruments DataFrame.
        strategies:     Dict mapping time_str → Strategy.
        broker:         BrokerClient for order placement.
        trade_logger:   TradeLogger for recording trade events.
        dynamo_store:   DynamoStateStore for persistent state (optional).
        notifier:       Callable(subject, message) for sending alerts (optional).
        settings:       Settings instance for configuration.
        ws:             WebSocket reference (set after construction via set_ws).
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
        self._kite = kite
        self._instruments_df = instruments_df
        self._strategies = strategies
        self._broker = broker
        self._trade_logger = trade_logger
        self._dynamo_store = dynamo_store
        self._notifier = notifier
        self._settings = settings
        self._ws: Optional[Any] = None

        # Per-strategy initial sum prices
        self._initial_sum: Dict[str, Optional[float]] = {}
        self._current_sum: Dict[str, Optional[float]] = {}
        self._price_diff: Dict[str, float] = {}

        # Trade margin tracking
        self._trade_margin: int = settings.trade_margin

        # Token subscription tracking (held externally in WebSocketManager)
        self._token_set: set[int] = set()

    def set_ws(self, ws: Any) -> None:
        """Attach the live WebSocket reference (called from WebSocketManager)."""
        self._ws = ws

    @property
    def strategies(self) -> Dict[str, Strategy]:
        return self._strategies

    def process_ticks(self, ticks: List[Dict[str, Any]]) -> None:
        """
        Main entry point called on every tick batch from KiteTicker.

        Args:
            ticks: List of raw tick dicts from KiteTicker.
        """
        set_correlation_id()
        current_time = datetime.now()
        current_time_str = current_time.strftime("%H:%M:%S")

        logger.debug("Processing tick batch at %s (%d ticks)", current_time_str, len(ticks))

        market_close = datetime.strptime(
            self._settings.market_close_time, "%H:%M:%S"
        ).time()

        # ── Market close: square off everything ──────────────────────────────
        if current_time.time() >= market_close:
            self._handle_market_close(ticks)
            return

        # ── Strategy lifecycle: pending / starting / skipping ─────────────
        all_completed = True
        for time_str, strategy in self._strategies.items():
            self._update_strategy_lifecycle(time_str, strategy, current_time)
            if not strategy.completed:
                all_completed = False

        # ── Execution: running strategies ─────────────────────────────────
        for time_str, strategy in self._strategies.items():
            if strategy.running:
                self._execute_strategy(time_str, strategy, ticks, current_time)

        # ── Early exit when everything is done ───────────────────────────
        if all_completed:
            logger.info("All strategies completed — closing websocket.")
            if self._ws is not None:
                self._ws.close()

    # ──────────────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _update_strategy_lifecycle(
        self,
        time_str: str,
        strategy: Strategy,
        current_time: datetime,
    ) -> None:
        """Handle PENDING → RUNNING / SKIPPED transition for a single strategy."""
        strategy_time = datetime.strptime(time_str, "%H:%M:%S").time()
        tolerance = self._settings.entry_tolerance_seconds

        tolerance_before = subtract_seconds_from_time(strategy_time, tolerance)
        tolerance_after = add_seconds_to_time(strategy_time, tolerance)

        # ── Time has elapsed without entry → skip ────────────────────────────
        if (
            current_time.time() > tolerance_after
            and not strategy.running
            and not strategy.completed
        ):
            logger.info("Skipping strategy %s — time elapsed.", time_str)
            strategy.completed = True
            strategy.skipped = True
            self._log_trade(strategy, time_str, TradeType.SKIPPED)
            return

        # ── Within entry window ───────────────────────────────────────────────
        in_window = tolerance_before <= current_time.time() <= tolerance_after
        if not in_window or strategy.running or strategy.completed:
            return

        # tradable=False → avoid strategy (DTE check)
        if not strategy.tradable:
            logger.info(
                "Avoiding strategy %s — tradable=False (DTE not 0).", time_str
            )
            strategy.running = False
            strategy.completed = True
            strategy.skipped = True
            self._log_trade(strategy, time_str, TradeType.AVOIDED)
            return

        # ── Start strategy ────────────────────────────────────────────────────
        logger.info("Starting strategy %s — index=%s", time_str, strategy.index)
        strategy.running = True
        self._initial_sum[time_str] = None
        strategy.current_ce_price = None
        strategy.current_pe_price = None

        # Refresh tokens with latest spot price
        ce_sym, pe_sym, ce_tok, pe_tok = get_tokens(
            self._kite, self._instruments_df, strategy.index, strategy.strangle
        )
        strategy.ce_symbol = ce_sym
        strategy.pe_symbol = pe_sym
        strategy.ce_token = ce_tok
        strategy.pe_token = pe_tok

        # Update WebSocket subscriptions
        if ce_tok and pe_tok and self._ws is not None:
            self._token_set.add(int(ce_tok))
            self._token_set.add(int(pe_tok))
            token_list = list(self._token_set)
            self._ws.subscribe(token_list)
            self._ws.set_mode(self._ws.MODE_FULL, token_list)
            logger.info("Subscribed to updated token list: %s", token_list)

    def _execute_strategy(
        self,
        time_str: str,
        strategy: Strategy,
        ticks: List[Dict[str, Any]],
        current_time: datetime,
    ) -> None:
        """Process ticks for a currently running strategy."""
        # Update CE/PE prices from ticks
        for tick in ticks:
            if tick["instrument_token"] == strategy.ce_token:
                strategy.current_ce_price = tick["last_price"]
            if tick["instrument_token"] == strategy.pe_token:
                strategy.current_pe_price = tick["last_price"]

            # Liquidity check — only on first tick (when liquidity_exists is None)
            if strategy.liquidity_exists is None:
                threshold = LIQUIDITY_THRESHOLDS.get(strategy.index, 0)
                is_liquid = check_liquidity(tick, threshold)
                if is_liquid:
                    strategy.liquidity_exists = True
                    logger.info("Liquidity confirmed for strategy %s", time_str)
                else:
                    logger.warning("No liquidity for strategy %s — skipping", time_str)
                    strategy.liquidity_exists = False
                    strategy.running = False
                    strategy.completed = True
                    strategy.skipped = True
                    strategy.current_ce_price = None
                    strategy.current_pe_price = None
                    self._initial_sum[time_str] = None
                    self._log_trade(strategy, time_str, TradeType.NO_LIQUIDITY)
                    return

        ce_price = strategy.current_ce_price
        pe_price = strategy.current_pe_price

        if ce_price is None or pe_price is None:
            return
        if not strategy.running or strategy.completed:
            return
        if strategy.liquidity_exists is not True:
            return

        self._current_sum[time_str] = ce_price + pe_price

        # ── Entry ──────────────────────────────────────────────────────────
        if self._initial_sum.get(time_str) is None:
            self._initial_sum[time_str] = self._current_sum[time_str]
            logger.info(
                "Entry — strategy=%s sum=%.2f ce=%.2f pe=%.2f",
                time_str,
                self._initial_sum[time_str],
                ce_price,
                pe_price,
            )
            self._handle_entry(time_str, strategy, ce_price, pe_price)
            return  # Don't check stop-loss on the entry tick

        # ── Stop-loss / take-profit exit ────────────────────────────────────
        if strategy.running and not strategy.completed:
            self._price_diff[time_str] = (
                self._current_sum[time_str] - self._initial_sum[time_str]
            )
            diff = self._price_diff[time_str]
            second = current_time.second

            if diff < 0:
                stop_loss = strategy.points
            else:
                stop_loss = strategy.points if second == 0 else 1.0 * strategy.points

            logger.debug(
                "Stop-loss check — strategy=%s diff=%.2f stop_loss=%.2f",
                time_str,
                diff,
                stop_loss,
            )

            if abs(diff) >= stop_loss:
                self._handle_exit(time_str, strategy, ce_price, pe_price)

    def _handle_entry(
        self,
        time_str: str,
        strategy: Strategy,
        ce_price: float,
        pe_price: float,
    ) -> None:
        """Place entry orders (SELL CE + SELL PE) or log for dummy trades."""
        if strategy.trade_type == TradeMode.DUMMY:
            if strategy.conditional_on is not None:
                dep_result = self._strategies[strategy.conditional_on].result
                if dep_result != StrategyResult.PROFIT:
                    logger.info(
                        "Dummy strategy %s skipped — conditional strategy not profitable",
                        time_str,
                    )
                    self._log_trade(
                        strategy,
                        time_str,
                        TradeType.NOT_EXECUTED_CONDITIONAL,
                        ce_price=ce_price,
                        pe_price=pe_price,
                        sum_price=self._current_sum.get(time_str, "-"),
                    )
                    self._close_strategy(time_str, strategy)
                    return
            # Dummy entry — just log
            self._log_trade(
                strategy,
                time_str,
                TradeType.ENTRY,
                ce_price=ce_price,
                pe_price=pe_price,
                sum_price=self._initial_sum.get(time_str),
            )
            return

        # ── Real trade entry ──────────────────────────────────────────────
        if strategy.conditional_on is not None:
            dep_result = self._strategies[strategy.conditional_on].result
            if dep_result != StrategyResult.PROFIT:
                logger.info(
                    "Real strategy %s not executed — conditional strategy not profitable",
                    time_str,
                )
                self._log_trade(
                    strategy,
                    time_str,
                    TradeType.NOT_EXECUTED_CONDITIONAL,
                    ce_price=ce_price,
                    pe_price=pe_price,
                    sum_price=self._current_sum.get(time_str, "-"),
                )
                self._close_strategy(time_str, strategy)
                return

        qty = self._broker.get_quantity(strategy.index)
        order_ce = self._broker.place_order(
            strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_SELL
        )
        order_pe = self._broker.place_order(
            strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_SELL
        )

        if order_ce is not None and order_pe is not None:
            self._trade_margin -= self._broker.get_margin_required(strategy.index)
            logger.info(
                "Entry orders placed — ce_order=%s pe_order=%s margin=%d",
                order_ce,
                order_pe,
                self._trade_margin,
            )
            self._log_trade(
                strategy,
                time_str,
                TradeType.ENTRY,
                ce_price=ce_price,
                pe_price=pe_price,
                sum_price=self._initial_sum.get(time_str),
            )

        elif order_ce is None and order_pe is not None:
            # Only PE filled — square off PE immediately
            logger.warning("Partial fill — only PE placed; squaring off PE.")
            self._broker.place_order(
                strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
            )
            self._log_trade(
                strategy,
                time_str,
                TradeType.IMPROPER_POSITION,
                ce_price=ce_price,
                pe_price=pe_price,
                sum_price=self._current_sum.get(time_str, "-"),
            )
            self._close_strategy(time_str, strategy, failed=True)

        elif order_ce is not None and order_pe is None:
            # Only CE filled — square off CE immediately
            logger.warning("Partial fill — only CE placed; squaring off CE.")
            self._broker.place_order(
                strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
            )
            self._log_trade(
                strategy,
                time_str,
                TradeType.IMPROPER_POSITION,
                ce_price=ce_price,
                pe_price=pe_price,
                sum_price=self._current_sum.get(time_str, "-"),
            )
            self._close_strategy(time_str, strategy, failed=True)

        else:
            # Neither leg filled
            logger.error("Both entry orders failed for strategy %s", time_str)
            self._log_trade(
                strategy,
                time_str,
                TradeType.FAILED,
                ce_price=ce_price,
                pe_price=pe_price,
                sum_price=self._current_sum.get(time_str, "-"),
            )
            self._close_strategy(time_str, strategy, failed=True)

    def _handle_exit(
        self,
        time_str: str,
        strategy: Strategy,
        ce_price: float,
        pe_price: float,
    ) -> None:
        """Place exit orders (BUY CE + BUY PE) or log for dummy trades."""
        diff = self._price_diff.get(time_str, 0.0)
        result = StrategyResult.PROFIT if diff < 0 else StrategyResult.LOSS

        if strategy.trade_type == TradeMode.DUMMY:
            self._log_trade(
                strategy,
                time_str,
                TradeType.EXIT,
                ce_price=ce_price,
                pe_price=pe_price,
                sum_price=self._current_sum.get(time_str),
                price_diff=diff,
            )
            strategy.running = False
            strategy.completed = True
            strategy.result = result
            strategy.current_ce_price = None
            strategy.current_pe_price = None
            self._initial_sum[time_str] = None
            logger.info("Dummy strategy %s exit — result=%s", time_str, result.value)
            return

        # ── Real trade exit ───────────────────────────────────────────────
        qty = self._broker.get_quantity(strategy.index)
        order_ce = self._broker.place_order(
            strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
        )
        order_pe = self._broker.place_order(
            strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
        )

        self._trade_margin += self._broker.get_margin_required(strategy.index)
        logger.info(
            "Exit orders placed — ce=%s pe=%s margin=%d result=%s",
            order_ce,
            order_pe,
            self._trade_margin,
            result.value,
        )

        self._log_trade(
            strategy,
            time_str,
            TradeType.EXIT,
            ce_price=ce_price,
            pe_price=pe_price,
            sum_price=self._current_sum.get(time_str),
            price_diff=diff,
        )
        strategy.running = False
        strategy.completed = True
        strategy.result = result
        strategy.current_ce_price = None
        strategy.current_pe_price = None
        self._initial_sum[time_str] = None

    def _handle_market_close(self, ticks: List[Dict[str, Any]]) -> None:
        """Square off all running strategies at market close."""
        logger.info("Market close reached — squaring off all positions.")

        for time_str, strategy in self._strategies.items():
            if not strategy.running:
                continue

            ce_price = strategy.current_ce_price
            pe_price = strategy.current_pe_price
            current_sum = (
                (ce_price + pe_price)
                if ce_price is not None and pe_price is not None
                else None
            )
            init_sum = self._initial_sum.get(time_str)
            diff = (current_sum - init_sum) if (current_sum is not None and init_sum is not None) else None
            result = StrategyResult.PROFIT if (diff is not None and diff < 0) else StrategyResult.LOSS

            if strategy.trade_type == TradeMode.REAL:
                qty = self._broker.get_quantity(strategy.index)
                self._broker.place_order(
                    strategy.ce_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
                )
                self._broker.place_order(
                    strategy.pe_symbol, qty, self._kite.TRANSACTION_TYPE_BUY
                )
                self._trade_margin += self._broker.get_margin_required(strategy.index)

            strategy.running = False
            strategy.completed = True
            strategy.result = result

            self._log_trade(
                strategy,
                time_str,
                TradeType.EXIT_AT_CLOSE,
                ce_price=ce_price if ce_price is not None else "-",
                pe_price=pe_price if pe_price is not None else "-",
                sum_price=current_sum if current_sum is not None else "N/A",
                price_diff=diff if diff is not None else "N/A",
            )

        logger.info("All positions squared off — closing websocket.")
        if self._ws is not None:
            self._ws.close()

    def _close_strategy(
        self,
        time_str: str,
        strategy: Strategy,
        failed: bool = False,
    ) -> None:
        """Mark a strategy as closed without placing exit orders."""
        strategy.running = False
        strategy.completed = True
        strategy.failed = failed
        strategy.current_ce_price = None
        strategy.current_pe_price = None
        self._initial_sum[time_str] = None

    def _log_trade(
        self,
        strategy: Strategy,
        time_str: str,
        event_type: TradeType,
        ce_price: Any = "-",
        pe_price: Any = "-",
        sum_price: Any = "-",
        price_diff: Any = "-",
    ) -> None:
        """Append a trade record to the trade logger."""
        record = TradeRecord(
            index=strategy.index,
            strangle=strategy.strangle,
            points=strategy.points,
            datetime=datetime.now(),
            trade_mode=strategy.trade_type.value,
            event_type=event_type.value,
            sum_price=sum_price,
            ce_price=ce_price,
            pe_price=pe_price,
            price_difference=price_diff,
            strategy_time_str=time_str,
        )
        self._trade_logger.log(record.to_dict())
