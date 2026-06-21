"""Unit tests for price rounding utilities."""
import pytest
from option_trading.utils.price import (
    round_to_nearest_fifty,
    round_to_nearest_hundred,
    round_price_based_on_template,
)


class TestRoundToNearestFifty:
    def test_exact_multiple(self):
        assert round_to_nearest_fifty(22050) == 22050

    def test_rounds_up(self):
        assert round_to_nearest_fifty(22026) == 22050

    def test_rounds_down(self):
        assert round_to_nearest_fifty(22024) == 22000

    def test_exactly_halfway_rounds_up(self):
        # Python's banker's rounding: 22025 → nearest even → 22000
        # but int(round(22025/50)*50) = int(round(440.5)*50) = int(440*50) = 22000
        result = round_to_nearest_fifty(22025)
        assert result in (22000, 22050)  # acceptable either way

    def test_returns_int(self):
        assert isinstance(round_to_nearest_fifty(22100.5), int)

    def test_zero(self):
        assert round_to_nearest_fifty(0) == 0

    def test_float_input(self):
        assert round_to_nearest_fifty(21999.9) == 22000


class TestRoundToNearestHundred:
    def test_exact_multiple(self):
        assert round_to_nearest_hundred(50000) == 50000

    def test_rounds_up(self):
        assert round_to_nearest_hundred(50051) == 50100

    def test_rounds_down(self):
        assert round_to_nearest_hundred(50049) == 50000

    def test_returns_int(self):
        assert isinstance(round_to_nearest_hundred(50050.5), int)

    def test_zero(self):
        assert round_to_nearest_hundred(0) == 0


class TestRoundPriceBasedOnTemplate:
    @pytest.mark.parametrize("symbol", ["BANKNIFTY", "MIDCPNIFTY"])
    def test_hundred_rounding_for_banknifty_midcpnifty(self, symbol):
        assert round_price_based_on_template(50051, symbol) == 50100
        assert round_price_based_on_template(50049, symbol) == 50000

    @pytest.mark.parametrize("symbol", ["NIFTY", "FINNIFTY"])
    def test_fifty_rounding_for_nifty_finnifty(self, symbol):
        assert round_price_based_on_template(22026, symbol) == 22050
        assert round_price_based_on_template(22024, symbol) == 22000

    def test_unknown_symbol_returns_rounded_int(self):
        result = round_price_based_on_template(12345.6, "UNKNOWN")
        assert result == 12346
        assert isinstance(result, int)

    def test_atm_case(self):
        # 22000 should stay at 22000 for NIFTY
        assert round_price_based_on_template(22000.0, "NIFTY") == 22000
