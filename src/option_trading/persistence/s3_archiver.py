"""
S3 archiver — uploads local files to an S3 bucket for long-term storage.
"""
from __future__ import annotations

from typing import Any, Optional

from option_trading.utils.logging_setup import get_logger

logger = get_logger(__name__)


class S3Archiver:
    """
    Archives local files to Amazon S3.

    Args:
        s3_client:   A boto3 S3 client (``boto3.client('s3')``).
        bucket_name: Target S3 bucket name.
    """

    def __init__(self, s3_client: Any, bucket_name: str) -> None:
        self._s3 = s3_client
        self._bucket = bucket_name

    def archive(
        self,
        local_path: str,
        s3_key: str,
        content_type: Optional[str] = None,
    ) -> bool:
        """
        Upload a local file to S3.

        Args:
            local_path:   Path to the local file.
            s3_key:       S3 object key (path within the bucket).
            content_type: Optional MIME type override.

        Returns:
            True on success, False on error.
        """
        extra_args: dict = {}
        if content_type:
            extra_args["ContentType"] = content_type

        try:
            self._s3.upload_file(
                local_path,
                self._bucket,
                s3_key,
                ExtraArgs=extra_args if extra_args else None,
            )
            logger.info(
                "Archived %s → s3://%s/%s", local_path, self._bucket, s3_key
            )
            return True
        except FileNotFoundError:
            logger.error("Local file not found: %s", local_path)
            return False
        except Exception as exc:
            logger.error(
                "Failed to archive %s to s3://%s/%s: %s",
                local_path,
                self._bucket,
                s3_key,
                exc,
            )
            return False
