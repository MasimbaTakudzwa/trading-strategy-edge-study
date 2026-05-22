"""Smoke tests for config loading and the practice/live switch."""

from __future__ import annotations

import pytest

from trading_bot.config import OANDA_HOSTS, OandaEnv, Settings


def test_defaults_to_practice() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.oanda_env == OandaEnv.PRACTICE
    assert s.is_live is False
    assert "fxpractice" in s.oanda_rest_url
    assert "fxpractice" in s.oanda_stream_url


def test_live_switch_changes_urls() -> None:
    s = Settings(_env_file=None, oanda_env=OandaEnv.LIVE)  # type: ignore[call-arg]
    assert s.is_live is True
    assert "fxtrade" in s.oanda_rest_url
    assert "fxpractice" not in s.oanda_rest_url


def test_hosts_table_covers_all_envs() -> None:
    for env in OandaEnv:
        assert env in OANDA_HOSTS
        assert "rest" in OANDA_HOSTS[env]
        assert "stream" in OANDA_HOSTS[env]


def test_postgres_dsn_constructed() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dsn = s.postgres_dsn
    assert dsn.startswith("postgresql+psycopg://")
    assert "tbot" in dsn


@pytest.mark.parametrize("pct", [-0.01, 0.5])
def test_max_account_risk_pct_validated(pct: float) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_account_risk_pct=pct)  # type: ignore[call-arg]
