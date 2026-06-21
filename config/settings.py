"""
Application settings loaded from environment variables / .env file.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_key: str = ""
    api_secret: str = ""
    kite_username: str = ""
    kite_password: str = ""
    totp_secret: str = ""
    trade_margin: int = 1000000
    n_strategies: int = 3
    aws_region: str = "ap-south-1"
    dynamodb_state_table: str = "trading-engine-state"
    dynamodb_trades_table: str = "trading-engine-trades"
    s3_bucket: str = "option-trading-data"
    sns_topic_arn: str = ""
    entry_tolerance_seconds: int = 15
    market_close_time: str = "15:30:00"
    strategies_csv: str = "GROUPED_tier_1_results.csv"
    trade_mode: str = "dummy"  # "dummy" | "real" — default safe

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
