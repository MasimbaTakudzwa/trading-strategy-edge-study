"""Application config loaded from .env and validated by pydantic.

CTRADER_ENV is the single switch between the demo and live cTrader hosts.
Demo/live share the same code path — only the host changes.

Auth model (different from OANDA's single-token approach):
  1. Register an Open API application at https://openapi.ctrader.com to get
     CTRADER_CLIENT_ID and CTRADER_CLIENT_SECRET. These identify your app.
  2. Run the OAuth flow to grant the app access to a specific trading
     account; that produces an access token (and a refresh token).
  3. CTRADER_ACCOUNT_ID is the numeric ctidTraderAccountId of the account
     you authorised (NOT the broker's human-readable account number).

See README "Setup" section for the full registration + OAuth walkthrough.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CTraderEnv(str, Enum):
    DEMO = "demo"
    LIVE = "live"


# cTrader Open API has two host pairs. The TCP+TLS port is 5035 for both;
# only the host changes.
CTRADER_HOSTS: dict[CTraderEnv, str] = {
    CTraderEnv.DEMO: "demo.ctraderapi.com",
    CTraderEnv.LIVE: "live.ctraderapi.com",
}
CTRADER_PORT = 5035


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # cTrader
    ctrader_env: CTraderEnv = CTraderEnv.DEMO
    ctrader_client_id: str = "replace-me"
    ctrader_client_secret: SecretStr = SecretStr("replace-me")
    ctrader_account_id: int = 0  # ctidTraderAccountId (numeric)
    ctrader_access_token: SecretStr = SecretStr("replace-me")
    ctrader_refresh_token: SecretStr = SecretStr("replace-me")

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
    def ctrader_host(self) -> str:
        return CTRADER_HOSTS[self.ctrader_env]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ctrader_port(self) -> int:
        return CTRADER_PORT

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_live(self) -> bool:
        return self.ctrader_env == CTraderEnv.LIVE

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
