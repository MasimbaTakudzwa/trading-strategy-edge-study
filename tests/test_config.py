"""Smoke tests for config loading and the demo/live switch."""

from __future__ import annotations

import pytest

from trading_bot.config import CTRADER_HOSTS, CTraderEnv, Settings


def test_defaults_to_demo() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.ctrader_env == CTraderEnv.DEMO
    assert s.is_live is False
    assert s.ctrader_host == "demo.ctraderapi.com"


def test_live_switch_changes_host() -> None:
    s = Settings(_env_file=None, ctrader_env=CTraderEnv.LIVE)  # type: ignore[call-arg]
    assert s.is_live is True
    assert s.ctrader_host == "live.ctraderapi.com"


def test_port_is_5035_for_both_envs() -> None:
    """cTrader uses the same TLS port for demo and live — only the host differs."""
    for env in CTraderEnv:
        s = Settings(_env_file=None, ctrader_env=env)  # type: ignore[call-arg]
        assert s.ctrader_port == 5035


def test_hosts_table_covers_all_envs() -> None:
    for env in CTraderEnv:
        assert env in CTRADER_HOSTS
        assert CTRADER_HOSTS[env].endswith("ctraderapi.com")


def test_postgres_dsn_constructed() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    dsn = s.postgres_dsn
    assert dsn.startswith("postgresql+psycopg://")
    assert "tbot" in dsn


@pytest.mark.parametrize("pct", [-0.01, 0.5])
def test_max_account_risk_pct_validated(pct: float) -> None:
    with pytest.raises(ValueError):
        Settings(_env_file=None, max_account_risk_pct=pct)  # type: ignore[call-arg]
