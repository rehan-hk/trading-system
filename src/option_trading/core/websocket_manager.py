"""
WebSocket manager — wraps KiteTicker and delegates ticks to StrategyEngine.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)

_MAX_RECONNECT_RETRIES = 5
_RECONNECT_INTERVAL = 5  # seconds


class WebSocketManager:
    """
    Manages the KiteTicker WebSocket connection lifecycle.

    The manager holds a reference to a StrategyEngine and routes all incoming
    tick batches to ``engine.process_ticks(ticks)``.

    Args:
        api_key:     Kite API key.
        access_token: Valid Kite access token.
        engine:      Initialised StrategyEngine instance.
        initial_tokens: List of instrument tokens to subscribe on connect.
    """

    def __init__(
        self,
        api_key: str,
        access_token: str,
        engine: Any,
        initial_tokens: Optional[List[int]] = None,
    ) -> None:
        from kiteconnect import KiteTicker  # lazy import

        self._engine = engine
        self._initial_tokens = list(initial_tokens or [])
        self._kws = KiteTicker(api_key, access_token)

        # Wire callbacks
        self._kws.on_ticks = self._on_ticks
        self._kws.on_connect = self._on_connect
        self._kws.on_close = self._on_close
        self._kws.on_reconnect = self._on_reconnect
        self._kws.on_error = self._on_error

        # Give the engine a reference to the raw KiteTicker so it can
        # subscribe/unsubscribe during strategy lifecycle changes.
        engine.set_ws(self._kws)

    def connect(self) -> None:
        """Start the WebSocket connection (blocking — runs the event loop)."""
        logger.info("Connecting WebSocket...")
        self._kws.connect()

    # ──────────────────────────────────────────────────────────────────────────
    # KiteTicker callbacks
    # ──────────────────────────────────────────────────────────────────────────

    def _on_ticks(self, ws: Any, ticks: List[dict]) -> None:
        try:
            self._engine.process_ticks(ticks)
        except Exception as exc:
            logger.error("Unhandled exception in process_ticks: %s", exc, exc_info=True)

    def _on_connect(self, ws: Any, response: Any) -> None:
        logger.info("WebSocket connected — subscribing to %d tokens", len(self._initial_tokens))
        if self._initial_tokens:
            ws.subscribe(self._initial_tokens)
            ws.set_mode(ws.MODE_FULL, self._initial_tokens)

    def _on_close(self, ws: Any, code: Any, reason: Any) -> None:
        logger.info("WebSocket closed — code=%s reason=%s", code, reason)
        ws.stop()

    def _on_reconnect(self, ws: Any, attemptsCount: int) -> None:
        logger.warning("WebSocket reconnecting — attempt %d", attemptsCount)

    def _on_error(self, ws: Any, code: Any, reason: Any) -> None:
        logger.error("WebSocket error — code=%s reason=%s", code, reason)
