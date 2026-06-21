"""
Strategy selection logic.

Reads from a CSV of back-tested results and selects the top *n* strategies
for today's day of the week, resolving tokens at selection time.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from option_trading.models.strategy import Strategy, TradeMode
from option_trading.services.instruments import TRADING_SYMBOL, get_tokens
from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)

# Reverse mapping: short symbol → NSE key
_REVERSE_TRADING_SYMBOL: Dict[str, str] = {v: k for k, v in TRADING_SYMBOL.items()}


def get_strategies_to_run_today(
    top_strategies_df: pd.DataFrame,
    n_strats: int,
    kite: Any,
    instruments_df: pd.DataFrame,
    as_of: Optional[datetime] = None,
    trade_mode: str = "dummy",
) -> Dict[str, Strategy]:
    """
    Select the top *n_strats* strategies for today's day of the week.

    On time conflict (two strategies with the same entry time), the one with
    the higher 'net profit' is kept.

    Args:
        top_strategies_df: DataFrame loaded from GROUPED_tier_1_results.csv.
        n_strats:          Number of strategies to select.
        kite:              Authenticated KiteConnect instance.
        instruments_df:    NFO instruments DataFrame.
        as_of:             Reference datetime; defaults to now.

    Returns:
        Dict keyed by time string (e.g. "10:21:00") mapping to Strategy objects.
    """
    as_of = as_of or datetime.now()
    todays_day = as_of.strftime("%A").lower()

    day_strats = top_strategies_df[top_strategies_df["day"] == todays_day]
    top_n = day_strats.nlargest(n_strats, "net profit")

    strategy_entry_times: Dict[str, Strategy] = {}

    for _, row in top_n.iterrows():
        time_key: str = row["max_success_time"]
        contract_type: str = row["contract type"]
        index = _REVERSE_TRADING_SYMBOL.get(contract_type)

        if index is None:
            logger.warning("Unknown contract type %r — skipping", contract_type)
            continue

        exit_point = float(row["exit_point"])
        strangle_str: str = str(row["strangle"]).replace("+-", "").strip()
        strangle = int(strangle_str)
        net_profit = float(row["net profit"])

        # Resolve tokens at selection time
        ce_symbol, pe_symbol, ce_token, pe_token = get_tokens(
            kite, instruments_df, index, strangle, as_of=as_of
        )

        mode = TradeMode.REAL if trade_mode == "real" else TradeMode.DUMMY
        strategy = Strategy(
            time_str=time_key,
            index=index,
            strangle=strangle,
            points=exit_point,
            trade_type=mode,
            conditional_on=None,
            ce_symbol=ce_symbol,
            pe_symbol=pe_symbol,
            ce_token=ce_token,
            pe_token=pe_token,
        )

        if time_key not in strategy_entry_times:
            strategy_entry_times[time_key] = strategy
            logger.info(
                "Selected strategy %s — index=%s strangle=%d points=%.1f",
                time_key,
                index,
                strangle,
                exit_point,
            )
        else:
            # Keep the higher net-profit strategy for the same time slot
            existing_net = float(
                top_n[top_n["max_success_time"] == time_key]["net profit"].max()
            )
            if net_profit > existing_net:
                strategy_entry_times[time_key] = strategy
                logger.info(
                    "Replaced strategy %s with higher net profit %.2f", time_key, net_profit
                )

    return strategy_entry_times
