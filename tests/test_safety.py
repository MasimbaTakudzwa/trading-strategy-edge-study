"""Tests for the trading-safety gates. These are the controls that stand
between the bot and an accidental live order — they get thorough coverage."""

from __future__ import annotations

import pytest

from trading_bot.config import CTraderEnv, Settings
from trading_bot.risk.safety import (
    LiveTradingBlocked,
    assert_can_trade,
    safety_posture,
)


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Demo environment — always safe (Layer 1 host isolation guarantees it)
# ---------------------------------------------------------------------------


def test_demo_env_is_always_allowed() -> None:
    assert_can_trade(_settings(ctrader_env=CTraderEnv.DEMO))


def test_demo_env_allowed_even_if_allow_live_flag_is_true() -> None:
    """ALLOW_LIVE_TRADING=true is meaningless under a demo env — still safe."""
    assert_can_trade(_settings(ctrader_env=CTraderEnv.DEMO, allow_live_trading=True))


def test_demo_env_with_demo_account_allowed() -> None:
    assert_can_trade(
        _settings(ctrader_env=CTraderEnv.DEMO), account_is_live=False
    )


# ---------------------------------------------------------------------------
# Layer 2 — explicit live switch
# ---------------------------------------------------------------------------


def test_live_env_blocked_without_switch() -> None:
    with pytest.raises(LiveTradingBlocked, match="ALLOW_LIVE_TRADING"):
        assert_can_trade(_settings(ctrader_env=CTraderEnv.LIVE, allow_live_trading=False))


def test_live_env_allowed_with_switch() -> None:
    assert_can_trade(_settings(ctrader_env=CTraderEnv.LIVE, allow_live_trading=True))


# ---------------------------------------------------------------------------
# Layer 3 — account reality check
# ---------------------------------------------------------------------------


def test_demo_env_with_live_account_is_blocked() -> None:
    """The dangerous case: demo env but the account is actually live."""
    with pytest.raises(LiveTradingBlocked, match="account is LIVE"):
        assert_can_trade(
            _settings(ctrader_env=CTraderEnv.DEMO), account_is_live=True
        )


def test_live_env_with_demo_account_is_allowed_but_harmless() -> None:
    """Inverse mismatch is harmless (no real money) — should not raise."""
    assert_can_trade(
        _settings(ctrader_env=CTraderEnv.LIVE, allow_live_trading=True),
        account_is_live=False,
    )


def test_live_account_under_demo_env_blocks_even_with_switch() -> None:
    """Layer 3 fires before Layer 2 — a live account under demo env is blocked
    regardless of the allow-live flag."""
    with pytest.raises(LiveTradingBlocked, match="account is LIVE"):
        assert_can_trade(
            _settings(ctrader_env=CTraderEnv.DEMO, allow_live_trading=True),
            account_is_live=True,
        )


# ---------------------------------------------------------------------------
# Posture summary
# ---------------------------------------------------------------------------


def test_posture_demo_reports_no_live_orders() -> None:
    posture = safety_posture(_settings(ctrader_env=CTraderEnv.DEMO))
    assert posture["env"] == "demo"
    assert posture["live_orders_possible"] is False


def test_posture_live_with_switch_reports_live_possible() -> None:
    posture = safety_posture(
        _settings(ctrader_env=CTraderEnv.LIVE, allow_live_trading=True)
    )
    assert posture["live_orders_possible"] is True


def test_posture_live_without_switch_reports_no_live_orders() -> None:
    posture = safety_posture(
        _settings(ctrader_env=CTraderEnv.LIVE, allow_live_trading=False)
    )
    assert posture["live_orders_possible"] is False


def test_default_config_cannot_trade_live() -> None:
    """The shipped default (.env.example) must never permit live orders."""
    posture = safety_posture(_settings())
    assert posture["live_orders_possible"] is False
