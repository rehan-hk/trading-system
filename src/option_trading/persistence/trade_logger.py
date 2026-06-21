"""
Trade logger — accumulates in-memory trade records and persists to CSV / DynamoDB.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import pandas as pd

from option_trading.utils.logging_setup import get_logger

if TYPE_CHECKING:
    from option_trading.persistence.dynamo_state import DynamoStateStore

logger = get_logger(__name__)


class TradeLogger:
    """
    Accumulates trade event records during a session and provides
    serialisation to CSV and DynamoDB.
    """

    def __init__(self) -> None:
        self._records: List[Dict[str, Any]] = []

    def log(self, record: Dict[str, Any]) -> None:
        """Append a trade event record."""
        self._records.append(record)
        logger.debug("Trade logged — type=%s", record.get("type", "unknown"))

    @property
    def records(self) -> List[Dict[str, Any]]:
        """Read-only view of all accumulated records."""
        return list(self._records)

    def to_dataframe(self) -> pd.DataFrame:
        """Return records as a pandas DataFrame."""
        return pd.DataFrame(self._records)

    def save_csv(self, path: str) -> bool:
        """
        Save all records to a CSV file.

        Args:
            path: File path to write.

        Returns:
            True on success, False on error.
        """
        try:
            df = self.to_dataframe()
            df.to_csv(path, index=False)
            logger.info("Trade data saved to %s (%d records)", path, len(df))
            return True
        except Exception as exc:
            logger.error("Failed to save trade CSV to %s: %s", path, exc)
            return False

    def save_to_dynamo(
        self,
        store: "DynamoStateStore",
        trading_date: Optional[str] = None,
    ) -> bool:
        """
        Save all trade records to DynamoDB, keyed by trading date and strategy time.

        Each record is stored individually under the strategy's time key.

        Args:
            store:        DynamoStateStore instance.
            trading_date: ISO date string; defaults to today.

        Returns:
            True if all records were saved, False if any failed.
        """
        trading_date = trading_date or datetime.now().strftime("%Y-%m-%d")
        success = True

        for i, record in enumerate(self._records):
            strategy_time = record.get("strategy_time_str", f"record_{i}")
            ok = store.save_strategy_state(
                trading_date=trading_date,
                strategy_time=strategy_time,
                state_dict=record,
            )
            if not ok:
                success = False

        return success
