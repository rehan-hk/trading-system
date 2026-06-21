"""
Expiry date calculation for NSE index options.

Supported indices:
    BANKNIFTY  — weekly expiry on Wednesday
    FINNIFTY   — weekly expiry on Tuesday
    NIFTY      — weekly expiry on Thursday
    MIDCPNIFTY — weekly expiry on Monday
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict


def next_day(given_date: date, weekday: int) -> date:
    """
    Advance *given_date* until it lands on *weekday* (0=Mon … 6=Sun).

    If *given_date* is already the target weekday it is returned as-is.
    """
    while given_date.weekday() != weekday:
        given_date += timedelta(days=1)
    return given_date


def next_monday(given_date: date) -> date:
    """Return the next (or same-day) Monday from *given_date*."""
    return next_day(given_date, 0)


def next_tuesday(given_date: date) -> date:
    """Return the next (or same-day) Tuesday from *given_date*."""
    return next_day(given_date, 1)


def next_wednesday(given_date: date) -> date:
    """Return the next (or same-day) Wednesday from *given_date*."""
    return next_day(given_date, 2)


def next_thursday(given_date: date) -> date:
    """Return the next (or same-day) Thursday from *given_date*."""
    return next_day(given_date, 3)


def get_expiry_dates(contract_type: str, given_date: date) -> Dict[str, date]:
    """
    Return the two nearest expiry dates for *contract_type* starting from *given_date*.

    Args:
        contract_type: One of 'BANKNIFTY', 'FINNIFTY', 'NIFTY', 'MIDCPNIFTY'.
        given_date:    The reference date (typically today).

    Returns:
        Dict with keys 'expiry1' (nearest) and 'expiry2' (following week).

    Raises:
        ValueError: If *contract_type* is not recognised.
    """
    if isinstance(given_date, datetime):
        given_date = given_date.date()

    if contract_type == "BANKNIFTY":
        expiry1 = next_wednesday(given_date)
        expiry2 = next_wednesday(expiry1 + timedelta(days=1))
    elif contract_type == "FINNIFTY":
        expiry1 = next_tuesday(given_date)
        expiry2 = next_tuesday(expiry1 + timedelta(days=1))
    elif contract_type == "NIFTY":
        expiry1 = next_thursday(given_date)
        expiry2 = next_thursday(expiry1 + timedelta(days=1))
    elif contract_type == "MIDCPNIFTY":
        expiry1 = next_monday(given_date)
        expiry2 = next_monday(expiry1 + timedelta(days=1))
    else:
        raise ValueError(f"Unknown contract type: {contract_type!r}")

    return {"expiry1": expiry1, "expiry2": expiry2}


def format_date(d: date) -> str:
    """
    Format a date as the NSE option symbol date component.

    Format:  YY<MONTH-WITHOUT-LEADING-ZERO>DD
    Example: date(2024, 6, 21) → "24621"

    Note: The original code uses ``d.strftime("%m").lstrip('0')`` for month
    and ``d.strftime("%d")`` (with leading zero kept) for day, producing
    strings like "24621" for June 21 2024.
    """
    year = d.strftime("%y")
    month = d.strftime("%m").lstrip("0")
    day = d.strftime("%d")
    return f"{year}{month}{day}"
