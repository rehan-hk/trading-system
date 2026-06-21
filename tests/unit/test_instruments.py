"""Unit tests for token / instrument resolution."""
from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock

import pandas as pd
import pytest

from option_trading.services.instruments import (
    get_tokens,
    number_of_weeks_in_month,
    week_of_month,
)


def _build_instruments_df() -> pd.DataFrame:
    """
    Minimal DataFrame covering weekly and monthly NIFTY options.

    Symbol format produced by format_date + TRADING_SYMBOL:
        format_date(2024-06-20) = "24620"
        monthly format = "24JUN"
        CE: NIFTY{expiry}{strike}CE  e.g. NIFTY2462022000CE
    """
    return pd.DataFrame(
        [
            # Weekly expiry — Thursday 2024-06-20
            # format_date(date(2024,6,20)) == "24620" → symbol "NIFTY2462022000CE"
            {
                "tradingsymbol": "NIFTY2462022000CE",
                "instrument_token": 2001,
                "expiry": pd.Timestamp(date(2024, 6, 20)),
                "strike": 22000.0,
                "instrument_type": "CE",
            },
            {
                "tradingsymbol": "NIFTY2462022000PE",
                "instrument_token": 2002,
                "expiry": pd.Timestamp(date(2024, 6, 20)),
                "strike": 22000.0,
                "instrument_type": "PE",
            },
            # Monthly expiry — "24JUN" → symbol "NIFTY24JUN22000CE"
            {
                "tradingsymbol": "NIFTY24JUN22000CE",
                "instrument_token": 2003,
                "expiry": pd.Timestamp(date(2024, 6, 27)),
                "strike": 22000.0,
                "instrument_type": "CE",
            },
            {
                "tradingsymbol": "NIFTY24JUN22000PE",
                "instrument_token": 2004,
                "expiry": pd.Timestamp(date(2024, 6, 27)),
                "strike": 22000.0,
                "instrument_type": "PE",
            },
        ]
    )


@pytest.fixture
def instruments_df() -> pd.DataFrame:
    return _build_instruments_df()


@pytest.fixture
def kite() -> MagicMock:
    mock = MagicMock()
    mock.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 22000.0}}
    return mock


class TestGetTokensFirstExpiry:
    def test_returns_weekly_symbols_and_tokens(self, kite, instruments_df):
        # Monday 2024-06-17 → next Thursday = 2024-06-20
        # format_date(2024-06-20) = "24620" → symbol "NIFTY2462022000CE"
        as_of = datetime(2024, 6, 17, 10, 0, 0)
        ce_sym, pe_sym, ce_tok, pe_tok = get_tokens(
            kite, instruments_df, "NSE:NIFTY 50", 0, as_of=as_of
        )
        assert ce_sym == "NIFTY2462022000CE"
        assert pe_sym == "NIFTY2462022000PE"
        assert ce_tok == 2001
        assert pe_tok == 2002

    def test_strangle_offsets_strike(self, instruments_df):
        """When strangle != 0, CE strike goes up, PE strike goes down."""
        # format_date(2024-06-20) = "24620"
        # ce_strike = 22200, pe_strike = 21800
        df = pd.DataFrame(
            [
                {
                    "tradingsymbol": "NIFTY2462022200CE",
                    "instrument_token": 5001,
                    "expiry": pd.Timestamp(date(2024, 6, 20)),
                    "strike": 22200.0,
                    "instrument_type": "CE",
                },
                {
                    "tradingsymbol": "NIFTY2462021800PE",
                    "instrument_token": 5002,
                    "expiry": pd.Timestamp(date(2024, 6, 20)),
                    "strike": 21800.0,
                    "instrument_type": "PE",
                },
            ]
        )
        kite_mock = MagicMock()
        kite_mock.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 22000.0}}
        as_of = datetime(2024, 6, 17)
        ce_sym, pe_sym, ce_tok, pe_tok = get_tokens(
            kite_mock, df, "NSE:NIFTY 50", 200, as_of=as_of
        )
        assert ce_sym == "NIFTY2462022200CE"
        assert pe_sym == "NIFTY2462021800PE"


class TestGetTokensMonthlyFallback:
    def test_monthly_fallback_in_last_two_weeks(self, kite):
        """When weekly not found and we are in week >= num_weeks-1, use monthly."""
        # 2024-06-24 is the 4th Monday of June — last 2 weeks of a 5-week month
        # Monthly format: year%100=24, month.upper()="JUN" → expiry_2="24JUN"
        # Symbol: "NIFTY24JUN22000CE"
        as_of = datetime(2024, 6, 24, 10, 0, 0)

        # Only monthly symbols in the dataframe
        df = pd.DataFrame(
            [
                {
                    "tradingsymbol": "NIFTY24JUN22000CE",
                    "instrument_token": 2003,
                    "expiry": pd.Timestamp(date(2024, 6, 27)),
                    "strike": 22000.0,
                    "instrument_type": "CE",
                },
                {
                    "tradingsymbol": "NIFTY24JUN22000PE",
                    "instrument_token": 2004,
                    "expiry": pd.Timestamp(date(2024, 6, 27)),
                    "strike": 22000.0,
                    "instrument_type": "PE",
                },
            ]
        )
        kite.ltp.return_value = {"NSE:NIFTY 50": {"last_price": 22000.0}}

        ce_sym, pe_sym, ce_tok, pe_tok = get_tokens(
            kite, df, "NSE:NIFTY 50", 0, as_of=as_of
        )
        assert ce_sym == "NIFTY24JUN22000CE"
        assert pe_sym == "NIFTY24JUN22000PE"
        assert ce_tok == 2003
        assert pe_tok == 2004


class TestGetTokensNotFound:
    def test_returns_none_tuple_when_not_found(self, kite):
        as_of = datetime(2024, 6, 17, 10, 0, 0)
        empty_df = pd.DataFrame(
            columns=["tradingsymbol", "instrument_token", "expiry", "strike"]
        )
        result = get_tokens(kite, empty_df, "NSE:NIFTY 50", 0, as_of=as_of)
        assert result == (None, None, None, None)

    def test_unknown_index_returns_none_tuple(self, kite, instruments_df):
        as_of = datetime(2024, 6, 17)
        result = get_tokens(kite, instruments_df, "NSE:UNKNOWN", 0, as_of=as_of)
        assert result == (None, None, None, None)


class TestHelpers:
    def test_week_of_month_first_week(self):
        # June 2024 starts on Saturday (weekday=5).
        # week_of_month formula: adjusted_dom = dom + first_day.weekday()
        # For June 1: adjusted = 1 + 5 = 6 → ceil(6/7) = 1 ✓
        dt = datetime(2024, 6, 1)
        assert week_of_month(dt) == 1

    def test_week_of_month_last_week(self):
        dt = datetime(2024, 6, 24)  # 4th Monday
        w = week_of_month(dt)
        assert w >= 4

    def test_number_of_weeks_in_month_june_2024(self):
        dt = datetime(2024, 6, 1)
        weeks = number_of_weeks_in_month(dt)
        assert weeks == 5  # June 2024 has 5 weeks
