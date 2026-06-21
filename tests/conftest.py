"""
Shared pytest fixtures for unit and integration tests.
"""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from config.settings import Settings


# ─── Settings fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def mock_settings() -> Settings:
    return Settings(
        api_key="test_api_key",
        api_secret="test_api_secret",
        kite_username="test_user",
        kite_password="test_pass",
        totp_secret="JBSWY3DPEHPK3PXP",
        trade_margin=1_000_000,
        n_strategies=3,
        aws_region="ap-south-1",
        dynamodb_state_table="test-state-table",
        dynamodb_trades_table="test-trades-table",
        s3_bucket="test-bucket",
        sns_topic_arn="",
        entry_tolerance_seconds=15,
        market_close_time="15:30:00",
        strategies_csv="GROUPED_tier_1_results.csv",
    )


# ─── Instruments DataFrame fixture ────────────────────────────────────────────

def _make_option_row(
    symbol: str,
    token: int,
    expiry: date,
    strike: int,
    option_type: str,
    segment: str = "NFO-OPT",
) -> dict:
    return {
        "instrument_token": token,
        "tradingsymbol": symbol,
        "expiry": pd.Timestamp(expiry),
        "strike": float(strike),
        "instrument_type": option_type,
        "segment": segment,
        "exchange": "NFO",
        "lot_size": 25,
    }


@pytest.fixture
def mock_instruments_df() -> pd.DataFrame:
    """
    Minimal NFO instruments DataFrame covering all four indices.

    Spot prices assumed:
        BANKNIFTY  → 50000 (rounded to 50000, 100-step)
        NIFTY      → 22000 (rounded to 22000, 50-step)
        FINNIFTY   → 21000 (rounded to 21000, 50-step)
        MIDCPNIFTY → 10000 (rounded to 10000, 100-step)

    Expiry dates (relative to a hypothetical Monday 2024-06-17):
        BANKNIFTY  → next Wed = 2024-06-19
        FINNIFTY   → next Tue = 2024-06-18
        NIFTY      → next Thu = 2024-06-20
        MIDCPNIFTY → next Mon = 2024-06-17
    """
    rows = [
        # BANKNIFTY CE / PE — weekly Wed expiry
        _make_option_row("BANKNIFTY24619500000CE", 1001, date(2024, 6, 19), 50000, "CE"),
        _make_option_row("BANKNIFTY24619500000PE", 1002, date(2024, 6, 19), 50000, "PE"),
        # BANKNIFTY monthly expiry
        _make_option_row("BANKNIFTYJUN2450000CE", 1003, date(2024, 6, 26), 50000, "CE"),
        _make_option_row("BANKNIFTYJUN2450000PE", 1004, date(2024, 6, 26), 50000, "PE"),
        # NIFTY CE / PE — weekly Thu expiry
        _make_option_row("NIFTY24620220000CE", 2001, date(2024, 6, 20), 22000, "CE"),
        _make_option_row("NIFTY24620220000PE", 2002, date(2024, 6, 20), 22000, "PE"),
        # NIFTY monthly expiry
        _make_option_row("NIFTYJUN2422000CE", 2003, date(2024, 6, 27), 22000, "CE"),
        _make_option_row("NIFTYJUN2422000PE", 2004, date(2024, 6, 27), 22000, "PE"),
        # FINNIFTY CE / PE — weekly Tue expiry
        _make_option_row("FINNIFTY24618210000CE", 3001, date(2024, 6, 18), 21000, "CE"),
        _make_option_row("FINNIFTY24618210000PE", 3002, date(2024, 6, 18), 21000, "PE"),
        # MIDCPNIFTY CE / PE — weekly Mon expiry
        _make_option_row("MIDCPNIFTY2461710000CE", 4001, date(2024, 6, 17), 10000, "CE"),
        _make_option_row("MIDCPNIFTY2461710000PE", 4002, date(2024, 6, 17), 10000, "PE"),
        # ATM strangle symbols used in integration test (NIFTY 0-strangle = ATM)
        _make_option_row("NIFTY24620220000CE", 2001, date(2024, 6, 20), 22000, "CE"),
        _make_option_row("NIFTY24620220000PE", 2002, date(2024, 6, 20), 22000, "PE"),
    ]
    return pd.DataFrame(rows).drop_duplicates(subset=["tradingsymbol"])


# ─── KiteConnect mock fixture ─────────────────────────────────────────────────

@pytest.fixture
def mock_kite() -> MagicMock:
    """Mock KiteConnect with realistic LTP responses."""
    kite = MagicMock()

    def ltp_side_effect(instrument: str) -> dict:
        prices = {
            "NSE:NIFTY BANK": 50000.0,
            "NSE:NIFTY 50": 22000.0,
            "NSE:NIFTY FIN SERVICE": 21000.0,
            "NSE:NIFTY MID SELECT": 10000.0,
        }
        price = prices.get(instrument, 0.0)
        return {instrument: {"last_price": price}}

    kite.ltp.side_effect = ltp_side_effect
    kite.place_order.return_value = "ORDER123456"

    # Kite constants
    kite.VARIETY_REGULAR = "regular"
    kite.EXCHANGE_NFO = "NFO"
    kite.PRODUCT_MIS = "MIS"
    kite.TRANSACTION_TYPE_BUY = "BUY"
    kite.TRANSACTION_TYPE_SELL = "SELL"

    return kite


# ─── Minimal tick helpers ─────────────────────────────────────────────────────

def make_tick(
    token: int,
    price: float,
    buy_qty: int = 5000,
    sell_qty: int = 5000,
    buy_price: float = 100.0,
    sell_price: float = 99.5,
) -> dict:
    """Build a minimal tick dict similar to what KiteTicker delivers."""
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
