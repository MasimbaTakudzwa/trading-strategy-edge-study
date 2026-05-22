"""Application config loaded from .env and validated by pydantic.

The OANDA_ENV variable is the single switch between practice and live trading.
The base URLs are derived from it — strategy and execution code never
hardcode a URL or a "is this live?" flag.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class OandaEnv(str, Enum):
    PRACTICE = "practice"
    LIVE = "live"


# OANDA's two distinct API endpoints. The free demo account uses the practice
# endpoint and the same code paths — only the host changes.
OANDA_HOSTS: dict[OandaEnv, dict[str, str]] = {
    OandaEnv.PRACTICE: {
        "rest": "https://api-fxpractice.oanda.com",
        "stream": "https://stream-fxpractice.oanda.com",
    },
    OandaEnv.LIVE: {
        "rest": "https://api-fxtrade.oanda.com",
        "stream": "https://stream-fxtrade.oanda.com",
    },
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # OANDA
    oanda_env: OandaEnv = OandaEnv.PRACTICE
    oanda_account_id: str = "000-000-00000000-000"
    oanda_api_token: SecretStr = SecretStr("replace-me")

    # Postgres
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_db: str = "tbot"
    postgres_user: str = "tbot"
    postgres_password: SecretStr = SecretStr("tbot")

    # Redis
    redis_url: str = "redis://localhost:6380/0"

    # Risk
    max_account_risk_pct: float = Field(0.005, ge=0.0, le=0.1)
    max_daily_loss_pct: float = Field(0.02, ge=0.0, le=0.5)
    max_open_positions: int = Field(5, ge=1, le=50)
    max_leverage: float = Field(5.0, ge=1.0, le=50.0)

    # Alerts
    slack_webhook_url: str = ""

    # Operational
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    heartbeat_seconds: int = 30

    @computed_field  # type: ignore[prop-decorator]
    @property
    def oanda_rest_url(self) -> str:
        return OANDA_HOSTS[self.oanda_env]["rest"]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def oanda_stream_url(self) -> str:
        return OANDA_HOSTS[self.oanda_env]["stream"]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        return self.oanda_env == OandaEnv.LIVE

    @computed_field  # type: ignore[prop-decorator]
    @property
    def postgres_dsn(self) -> str:
        pw = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pw}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
