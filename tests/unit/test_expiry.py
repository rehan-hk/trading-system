"""Unit tests for expiry date calculation."""
from datetime import date

import pytest

from option_trading.services.expiry import (
    format_date,
    get_expiry_dates,
    next_day,
    next_monday,
    next_tuesday,
    next_wednesday,
    next_thursday,
)


class TestNextDay:
    def test_same_day_returns_same(self):
        # Monday (weekday=0)
        d = date(2024, 6, 17)
        assert next_day(d, 0) == date(2024, 6, 17)

    def test_advances_to_target(self):
        monday = date(2024, 6, 17)
        wednesday = next_day(monday, 2)
        assert wednesday == date(2024, 6, 19)
        assert wednesday.weekday() == 2


class TestGetExpiryDates:
    """Reference date: Monday 2024-06-17."""

    _ref = date(2024, 6, 17)

    def test_banknifty_expiry1_is_wednesday(self):
        result = get_expiry_dates("BANKNIFTY", self._ref)
        assert result["expiry1"] == date(2024, 6, 19)  # Wednesday
        assert result["expiry1"].weekday() == 2

    def test_banknifty_expiry2_is_following_wednesday(self):
        result = get_expiry_dates("BANKNIFTY", self._ref)
        assert result["expiry2"] == date(2024, 6, 26)

    def test_finnifty_expiry1_is_tuesday(self):
        result = get_expiry_dates("FINNIFTY", self._ref)
        assert result["expiry1"] == date(2024, 6, 18)
        assert result["expiry1"].weekday() == 1

    def test_finnifty_expiry2_is_following_tuesday(self):
        result = get_expiry_dates("FINNIFTY", self._ref)
        assert result["expiry2"] == date(2024, 6, 25)

    def test_nifty_expiry1_is_thursday(self):
        result = get_expiry_dates("NIFTY", self._ref)
        assert result["expiry1"] == date(2024, 6, 20)
        assert result["expiry1"].weekday() == 3

    def test_nifty_expiry2_is_following_thursday(self):
        result = get_expiry_dates("NIFTY", self._ref)
        assert result["expiry2"] == date(2024, 6, 27)

    def test_midcpnifty_expiry1_is_monday(self):
        result = get_expiry_dates("MIDCPNIFTY", self._ref)
        assert result["expiry1"] == date(2024, 6, 17)
        assert result["expiry1"].weekday() == 0

    def test_midcpnifty_expiry2_is_following_monday(self):
        result = get_expiry_dates("MIDCPNIFTY", self._ref)
        assert result["expiry2"] == date(2024, 6, 24)

    def test_invalid_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown contract type"):
            get_expiry_dates("INVALID", self._ref)

    def test_accepts_datetime_input(self):
        from datetime import datetime
        dt = datetime(2024, 6, 17, 9, 15, 0)
        result = get_expiry_dates("NIFTY", dt)
        assert result["expiry1"] == date(2024, 6, 20)

    def test_reference_date_on_expiry_day_returns_same_day(self):
        # If today is Wednesday, BANKNIFTY expiry1 should be today
        wednesday = date(2024, 6, 19)
        result = get_expiry_dates("BANKNIFTY", wednesday)
        assert result["expiry1"] == wednesday


class TestFormatDate:
    def test_june_21_2024(self):
        d = date(2024, 6, 21)
        # year=24, month=6 (no leading zero), day=21
        assert format_date(d) == "24621"

    def test_october_3_2024(self):
        d = date(2024, 10, 3)
        # year=24, month=10, day=03
        assert format_date(d) == "241003"

    def test_january_1_2025(self):
        d = date(2025, 1, 1)
        # year=25, month=1, day=01
        assert format_date(d) == "25101"

    def test_december_31_2024(self):
        d = date(2024, 12, 31)
        assert format_date(d) == "241231"
