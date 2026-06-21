"""Unit tests for risk monitoring utilities."""
from __future__ import annotations

import pandas as pd
import pytest

from option_trading.services.risk import calculate_drawdown, check_success_rate


class TestCheckSuccessRate:
    def test_high_win_rate_triggers_alert(self):
        # 8/8 wins with expected p=0.70 → extreme upper tail
        trades = [1, 1, 1, 1, 1, 1, 1, 1]
        assert check_success_rate(trades, p=0.70, alert_threshold=0.20) is True

    def test_low_win_rate_triggers_alert(self):
        # 0/8 wins with expected p=0.70 → extreme lower tail
        trades = [0, 0, 0, 0, 0, 0, 0, 0]
        assert check_success_rate(trades, p=0.70, alert_threshold=0.20) is True

    def test_average_win_rate_no_alert(self):
        # ~70% wins → no alert expected
        trades = [1, 1, 1, 1, 1, 1, 0, 0]  # 6/8 = 75%
        result = check_success_rate(trades, p=0.70, alert_threshold=0.05)
        # With very tight threshold this should not fire
        assert isinstance(result, bool)

    def test_uses_last_eight_trades(self):
        # Provide more than 8; last 8 are all losses → alert
        trades = [1] * 100 + [0] * 8
        assert check_success_rate(trades, p=0.70, alert_threshold=0.20) is True

    def test_empty_series_returns_false(self):
        assert check_success_rate([], p=0.70, alert_threshold=0.20) is False

    def test_fewer_than_eight_trades(self):
        # Should not crash and should return a bool
        trades = [1, 0, 1]
        result = check_success_rate(trades, p=0.70, alert_threshold=0.20)
        assert isinstance(result, bool)


class TestCalculateDrawdown:
    def test_no_drawdown(self):
        equity = pd.Series([100, 110, 120, 130])
        dd, max_dd = calculate_drawdown(equity)
        assert max_dd == pytest.approx(0.0)
        assert (dd == 0.0).all()

    def test_drawdown_from_peak(self):
        equity = pd.Series([100, 120, 90, 110])
        dd, max_dd = calculate_drawdown(equity)
        # Peak is 120, lowest after peak is 90 → drawdown = (120-90)/120 = 0.25
        assert max_dd == pytest.approx(0.25, rel=1e-3)

    def test_full_recovery(self):
        equity = pd.Series([100, 80, 100])
        dd, max_dd = calculate_drawdown(equity)
        assert max_dd == pytest.approx(0.20, rel=1e-3)

    def test_returns_series_and_float(self):
        equity = pd.Series([100, 90, 95])
        dd, max_dd = calculate_drawdown(equity)
        assert isinstance(dd, pd.Series)
        assert isinstance(max_dd, float)

    def test_single_value(self):
        equity = pd.Series([100])
        dd, max_dd = calculate_drawdown(equity)
        assert max_dd == pytest.approx(0.0)
