"""
Instrument / token resolution for NSE option symbols.

Given an index and a strangle offset this module resolves the CE and PE
trading symbols and Kite instrument tokens from the NFO instruments DataFrame.
"""
from __future__ import annotations

import calendar
from datetime import datetime
from math import ceil
from typing import Optional, Tuple

import pandas as pd

from option_trading.services.expiry import format_date, get_expiry_dates
from option_trading.utils.logging_setup import get_logger
from option_trading.utils.price import round_price_based_on_template

logger = get_logger(__name__)

# Mapping from NSE instrument key → short symbol used in option trading symbols
TRADING_SYMBOL: dict[str, str] = {
    "NSE:NIFTY FIN SERVICE": "FINNIFTY",
    "NSE:NIFTY 50": "NIFTY",
    "NSE:NIFTY BANK": "BANKNIFTY",
    "NSE:NIFTY MID SELECT": "MIDCPNIFTY",
}

# Reverse mapping: short symbol → NSE instrument key
INDEX_SYMBOL: dict[str, str] = {v: k for k, v in TRADING_SYMBOL.items()}


def week_of_month(dt: datetime) -> int:
    """
    Return the week-of-month number (1-indexed) for *dt*.

    Uses the ISO convention: the first partial week is week 1.
    """
    first_day = dt.replace(day=1)
    dom = dt.day
    adjusted_dom = dom + first_day.weekday()
    return int(ceil(adjusted_dom / 7.0))


def number_of_weeks_in_month(dt: datetime) -> int:
    """Return the total number of calendar weeks in the month of *dt*."""
    return len(calendar.monthcalendar(dt.year, dt.month))


def get_tokens(
    kite: object,
    instruments_df: pd.DataFrame,
    index: str,
    strangle: int,
    as_of: Optional[datetime] = None,
) -> Tuple[Optional[str], Optional[str], Optional[int], Optional[int]]:
    """
    Resolve CE and PE option symbols and instrument tokens for the given index.

    Strategy:
    1. Build the nearest weekly expiry symbol.
    2. Look it up in *instruments_df*.
    3. If found → return it.
    4. If not found AND we are in the last 2 weeks of the month → fall back
       to the monthly expiry symbol.
    5. If still not found → return (None, None, None, None).

    Args:
        kite:           An authenticated KiteConnect instance (used for LTP).
        instruments_df: DataFrame from ``kite.instruments("NFO")``.
        index:          NSE instrument key e.g. "NSE:NIFTY 50".
        strangle:       Distance from ATM for CE/PE strikes (0 = ATM).
        as_of:          Reference datetime; defaults to now.

    Returns:
        Tuple of (ce_symbol, pe_symbol, ce_token, pe_token) or four Nones.
    """
    as_of = as_of or datetime.now()

    short_sym = TRADING_SYMBOL.get(index)
    if short_sym is None:
        logger.error("Unknown index: %s", index)
        return None, None, None, None

    year = as_of.year % 100
    month = as_of.strftime("%b").upper()
    num_weeks = number_of_weeks_in_month(as_of)

    expiry_dates = get_expiry_dates(short_sym, as_of)
    expiry_1 = format_date(expiry_dates["expiry1"])
    # Monthly expiry format e.g. "24JUN"
    expiry_2 = f"{year}{month}"

    # Fetch current spot price and round to the appropriate strike interval
    ltp_data = kite.ltp(index)
    spot_p: float = ltp_data[index]["last_price"]
    spot_price = round_price_based_on_template(spot_p, short_sym)

    ce_strike = spot_price + strangle
    pe_strike = spot_price - strangle

    # --- Primary: nearest weekly expiry ---
    ce_sym_1 = f"{short_sym}{expiry_1}{ce_strike}CE"
    pe_sym_1 = f"{short_sym}{expiry_1}{pe_strike}PE"

    ce_opt = instruments_df[instruments_df["tradingsymbol"] == ce_sym_1]
    pe_opt = instruments_df[instruments_df["tradingsymbol"] == pe_sym_1]

    if not ce_opt.empty and not pe_opt.empty:
        logger.info("Using 1st (weekly) expiry: %s / %s", ce_sym_1, pe_sym_1)
        return (
            ce_sym_1,
            pe_sym_1,
            int(ce_opt["instrument_token"].values[0]),
            int(pe_opt["instrument_token"].values[0]),
        )

    logger.info("Weekly expiry options not found: %s / %s", ce_sym_1, pe_sym_1)

    # --- Fallback: monthly expiry — only in last 2 weeks ---
    if week_of_month(as_of) >= num_weeks - 1:
        ce_sym_2 = f"{short_sym}{expiry_2}{ce_strike}CE"
        pe_sym_2 = f"{short_sym}{expiry_2}{pe_strike}PE"

        ce_opt = instruments_df[instruments_df["tradingsymbol"] == ce_sym_2]
        pe_opt = instruments_df[instruments_df["tradingsymbol"] == pe_sym_2]

        if not ce_opt.empty and not pe_opt.empty:
            logger.info("Using monthly expiry: %s / %s", ce_sym_2, pe_sym_2)
            return (
                ce_sym_2,
                pe_sym_2,
                int(ce_opt["instrument_token"].values[0]),
                int(pe_opt["instrument_token"].values[0]),
            )

        logger.warning("Monthly expiry options also not found: %s / %s", ce_sym_2, pe_sym_2)

    logger.error("No token found for index=%s strangle=%s as_of=%s", index, strangle, as_of)
    return None, None, None, None
