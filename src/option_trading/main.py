"""
option_trading.main — application entry point.

Startup sequence:
    1. Load settings from environment / .env
    2. Configure structured logging
    3. Auto-login to Kite Connect
    4. Fetch NFO instruments into a DataFrame
    5. Load strategies from CSV using strategy_selector
    6. Set DTE tradability flags on each strategy
    7. Initialise persistence and notification services
    8. Start HTTP health-check server on :8080 (background thread)
    9. Register SIGTERM handler for graceful shutdown
   10. Create StrategyEngine and connect WebSocket
"""
from __future__ import annotations

import os
import signal
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import boto3
import pandas as pd

from config.settings import Settings
from option_trading.core.auth import autologin
from option_trading.core.broker import BrokerClient
from option_trading.core.strategy_engine import StrategyEngine
from option_trading.core.websocket_manager import WebSocketManager
from option_trading.persistence.dynamo_state import DynamoStateStore
from option_trading.persistence.s3_archiver import S3Archiver
from option_trading.persistence.trade_logger import TradeLogger
from option_trading.services.instruments import TRADING_SYMBOL, get_tokens
from option_trading.services.notifications import send_alert
from option_trading.services.strategy_selector import get_strategies_to_run_today
from option_trading.utils.logging_setup import configure_logging, get_logger

logger = get_logger(__name__)

_shutdown_event = threading.Event()


# ─── Health-check HTTP server ────────────────────────────────────────────────

class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Suppress default access log to keep stdout clean
        pass


def _start_health_server(port: int = 8080) -> HTTPServer:
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check server listening on :%d/health", port)
    return server


# ─── DTE tradability check ────────────────────────────────────────────────────

def _set_tradability_flags(strategies: dict, instruments_df: pd.DataFrame) -> None:
    """
    Set strategy.tradable = True only when both CE and PE expire today (DTE==0).
    """
    today = datetime.now().date()
    for strategy in strategies.values():
        ce_sym = strategy.ce_symbol
        pe_sym = strategy.pe_symbol
        if ce_sym is None or pe_sym is None:
            strategy.tradable = False
            continue

        ce_row = instruments_df[instruments_df["tradingsymbol"] == ce_sym]
        pe_row = instruments_df[instruments_df["tradingsymbol"] == pe_sym]

        if ce_row.empty or pe_row.empty:
            strategy.tradable = False
            continue

        ce_expiry = pd.Timestamp(ce_row["expiry"].values[0]).date()
        pe_expiry = pd.Timestamp(pe_row["expiry"].values[0]).date()

        strategy.tradable = (ce_expiry == today) and (pe_expiry == today)

        if not strategy.tradable:
            logger.info(
                "Strategy %s not tradable today — CE expiry=%s PE expiry=%s",
                strategy.time_str,
                ce_expiry,
                pe_expiry,
            )


# ─── Graceful shutdown ────────────────────────────────────────────────────────

def _handle_sigterm(signum: int, frame: object) -> None:
    logger.info("Received SIGTERM — initiating graceful shutdown.")
    _shutdown_event.set()
    sys.exit(0)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    # 1. Load settings
    settings = Settings()

    # 2. Configure logging
    configure_logging(level="INFO", log_file="app.log")
    logger.info("Starting option-trading engine.")

    # 3. Auto-login
    logger.info("Authenticating with Kite Connect...")
    access_token = autologin(
        api_key=settings.api_key,
        api_secret=settings.api_secret,
        username=settings.kite_username,
        password=settings.kite_password,
        totp_secret=settings.totp_secret,
    )

    # 4. Initialise Kite
    from kiteconnect import KiteConnect

    kite = KiteConnect(api_key=settings.api_key)
    kite.set_access_token(access_token)

    logger.info("Fetching NFO instruments...")
    instruments = kite.instruments("NFO")
    instruments_df = pd.DataFrame(instruments)
    logger.info("Loaded %d NFO instruments.", len(instruments_df))

    # 5. Load strategies
    top_strategies = pd.read_csv(settings.strategies_csv)
    strategies = get_strategies_to_run_today(
        top_strategies_df=top_strategies,
        n_strats=settings.n_strategies,
        kite=kite,
        instruments_df=instruments_df,
    )

    if not strategies:
        logger.warning("No strategies selected for today — exiting.")
        return

    # Mark skipped for strategies without tokens
    for time_str, strategy in strategies.items():
        if strategy.ce_token is None or strategy.pe_token is None:
            logger.warning("No token for strategy %s — marking skipped.", time_str)
            strategy.skipped = True
            strategy.completed = True

    # 6. Set DTE tradability
    _set_tradability_flags(strategies, instruments_df)

    # 7. Initialise persistence and notifications
    aws_session = boto3.Session(region_name=settings.aws_region)
    dynamo_resource = aws_session.resource("dynamodb")
    dynamo_store = DynamoStateStore(dynamo_resource, settings.dynamodb_state_table)

    s3_client = aws_session.client("s3")
    s3_archiver = S3Archiver(s3_client, settings.s3_bucket)

    sns_client = aws_session.client("sns")

    trade_logger = TradeLogger()

    def notifier(subject: str, message: str) -> None:
        send_alert(sns_client, settings.sns_topic_arn, subject, message)

    # 8. Health-check server
    health_server = _start_health_server(port=8080)

    # 9. SIGTERM handler
    signal.signal(signal.SIGTERM, _handle_sigterm)

    # 10. Build engine and connect WebSocket
    broker = BrokerClient(kite)
    engine = StrategyEngine(
        kite=kite,
        instruments_df=instruments_df,
        strategies=strategies,
        broker=broker,
        trade_logger=trade_logger,
        dynamo_store=dynamo_store,
        notifier=notifier,
        settings=settings,
    )

    initial_tokens: list[int] = []
    for s in strategies.values():
        if s.ce_token:
            initial_tokens.append(s.ce_token)
        if s.pe_token:
            initial_tokens.append(s.pe_token)

    ws_manager = WebSocketManager(
        api_key=settings.api_key,
        access_token=access_token,
        engine=engine,
        initial_tokens=initial_tokens,
    )

    try:
        logger.info("Connecting WebSocket — monitoring %d tokens.", len(initial_tokens))
        ws_manager.connect()
    finally:
        # On shutdown, save trade data
        trading_date = datetime.now().strftime("%Y%m%d")
        csv_path = f"all_trade_data_{trading_date}.csv"
        trade_logger.save_csv(csv_path)
        s3_archiver.archive(csv_path, f"trade-data/{csv_path}")
        trade_logger.save_to_dynamo(dynamo_store)
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
