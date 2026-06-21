"""
DynamoDB strategy state persistence.

Saves and loads per-strategy state so the engine can resume after a restart.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)


class DynamoStateStore:
    """
    Read/write strategy state to DynamoDB.

    Table schema:
        Partition key: trading_date (String, "YYYY-MM-DD")
        Sort key:      strategy_time (String, e.g. "11:24:40")
        Attribute:     state (Map — the serialised strategy fields)
        TTL attribute: ttl (Number, Unix epoch, auto-set to 7 days)

    Args:
        dynamo_resource: boto3 DynamoDB resource (``boto3.resource('dynamodb')``)
        table_name:      DynamoDB table name.
    """

    def __init__(self, dynamo_resource: Any, table_name: str) -> None:
        self._table = dynamo_resource.Table(table_name)
        self._table_name = table_name

    def save_strategy_state(
        self,
        trading_date: str,
        strategy_time: str,
        state_dict: Dict[str, Any],
    ) -> bool:
        """
        Persist a single strategy's state.

        Args:
            trading_date:  ISO date string e.g. "2024-06-21".
            strategy_time: Time string key e.g. "11:24:40".
            state_dict:    Dict representation of StrategyState.

        Returns:
            True on success, False on error.
        """
        import time as _time

        ttl = int(_time.time()) + 7 * 24 * 3600  # 7-day TTL

        # DynamoDB requires all floats/ints as Decimal for serialisation
        from decimal import Decimal
        safe_state = _deep_convert(state_dict)

        try:
            self._table.put_item(
                Item={
                    "trading_date": trading_date,
                    "strategy_time": strategy_time,
                    "state": safe_state,
                    "ttl": ttl,
                    "updated_at": datetime.utcnow().isoformat(),
                }
            )
            logger.debug("Saved state for %s / %s", trading_date, strategy_time)
            return True
        except Exception as exc:
            logger.error(
                "Failed to save state for %s / %s: %s", trading_date, strategy_time, exc
            )
            return False

    def load_strategy_states(self, trading_date: str) -> Dict[str, Dict[str, Any]]:
        """
        Load all strategy states for a given trading date.

        Args:
            trading_date: ISO date string e.g. "2024-06-21".

        Returns:
            Dict mapping strategy_time → state_dict. Empty dict if none found.
        """
        from boto3.dynamodb.conditions import Key

        try:
            response = self._table.query(
                KeyConditionExpression=Key("trading_date").eq(trading_date)
            )
            items = response.get("Items", [])
            return {item["strategy_time"]: item.get("state", {}) for item in items}
        except Exception as exc:
            logger.error("Failed to load states for %s: %s", trading_date, exc)
            return {}


def _deep_convert(obj: Any) -> Any:
    """Recursively convert float/int to Decimal for DynamoDB compatibility."""
    from decimal import Decimal

    if isinstance(obj, dict):
        return {k: _deep_convert(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_deep_convert(v) for v in obj]
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, bool):
        return obj  # must check bool before int (bool is subclass of int)
    if isinstance(obj, int):
        return Decimal(obj)
    return obj
