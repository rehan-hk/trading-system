"""
AWS SNS notification service.

Sends alert messages through an SNS topic when risk thresholds are
breached or system events require human attention.
"""
from __future__ import annotations

from typing import Any

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)


def send_alert(
    sns_client: Any,
    topic_arn: str,
    subject: str,
    message: str,
) -> bool:
    """
    Publish an alert message to an SNS topic.

    Args:
        sns_client: A boto3 SNS client.
        topic_arn:  ARN of the target SNS topic.
        subject:    Message subject (used for email subscriptions, max 100 chars).
        message:    Message body.

    Returns:
        True on success, False on failure.
    """
    if not topic_arn:
        logger.debug("SNS topic ARN not configured — skipping notification.")
        return False

    try:
        response = sns_client.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=message,
        )
        message_id = response.get("MessageId", "unknown")
        logger.info("SNS alert sent — subject=%r message_id=%s", subject, message_id)
        return True
    except Exception as exc:
        logger.error("Failed to send SNS alert: %s", exc)
        return False
