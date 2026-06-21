"""
Risk monitoring utilities.

Implements binomial-test based success-rate alerting and equity drawdown
calculation to flag when trading performance deteriorates.
"""
from __future__ import annotations

from typing import Sequence, Tuple

import pandas as pd
from scipy.stats import binom

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)


def check_success_rate(
    trade_results_series: Sequence[int],
    p: float = 0.70,
    alert_threshold: float = 0.20,
) -> bool:
    """
    Run a one-tailed binomial test on the last 8 trades.

    Args:
        trade_results_series: Sequence of 0/1 values (1 = profit, 0 = loss).
        p:                    Expected success probability (default 70 %).
        alert_threshold:      p-value threshold that triggers an alert.

    Returns:
        True if the success rate deviates significantly from the expected *p*
        (either suspiciously low or suspiciously high), False otherwise.
    """
    trades = list(trade_results_series)[-8:]
    if not trades:
        return False

    n = len(trades)
    k = sum(trades)
    p_value = float(binom.cdf(k, n, p))

    logger.info(
        "Success rate check — n=%d k=%d p_value=%.4f threshold=%.2f",
        n,
        k,
        p_value,
        alert_threshold,
    )

    return p_value < alert_threshold or p_value > (1.0 - alert_threshold)


def calculate_drawdown(
    equity_series: pd.Series,
) -> Tuple[pd.Series, float]:
    """
    Calculate the running drawdown from peak equity.

    Args:
        equity_series: Pandas Series of equity values over time.

    Returns:
        Tuple of (drawdown_series, max_drawdown) where drawdown values are
        expressed as fractions (0.10 = 10 %).
    """
    peak = equity_series.expanding().max()
    drawdown = (peak - equity_series) / peak.replace(0, float("nan"))
    drawdown = drawdown.fillna(0.0)
    max_drawdown = float(drawdown.max())

    logger.info("Max drawdown: %.2f%%", max_drawdown * 100)
    return drawdown, max_drawdown


def check_trading_performance(
    trade_data: pd.DataFrame,
    p: float = 0.70,
    alert_threshold: float = 0.20,
    drawdown_alert_threshold: float = 0.10,
) -> dict:
    """
    Combined performance check — success rate and drawdown.

    Args:
        trade_data:               DataFrame with 'Trade_Result' (0/1) and 'Equity' columns.
        p:                        Expected win probability.
        alert_threshold:          Binomial p-value alert boundary.
        drawdown_alert_threshold: Maximum acceptable drawdown fraction.

    Returns:
        Dict with keys 'success_rate_alert' (bool) and 'drawdown_alert' (bool).
    """
    success_rate_alert = check_success_rate(
        trade_data["Trade_Result"], p=p, alert_threshold=alert_threshold
    )
    _, max_drawdown = calculate_drawdown(trade_data["Equity"])
    drawdown_alert = max_drawdown > drawdown_alert_threshold

    if success_rate_alert:
        logger.warning("ALERT: Success rate is deviating from the expected %.0f%%!", p * 100)
    if drawdown_alert:
        logger.warning(
            "ALERT: Drawdown %.1f%% exceeds limit of %.1f%%!",
            max_drawdown * 100,
            drawdown_alert_threshold * 100,
        )

    return {"success_rate_alert": success_rate_alert, "drawdown_alert": drawdown_alert}
