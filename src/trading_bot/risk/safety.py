"""Trading safety gates — the last line of defence before any live order.

Defence in depth, three independent layers:

  Layer 1 — Host isolation (enforced by cTrader, not us). CTRADER_ENV=demo
    connects to demo.ctraderapi.com, which only serves demo accounts. A demo
    environment physically cannot place an order on a live account, even if
    every other check were bypassed.

  Layer 2 — Explicit live switch. Even with CTRADER_ENV=live, order placement
    is refused unless ALLOW_LIVE_TRADING=true. Going live is a deliberate
    two-key turn, never a single env var flipped by accident.

  Layer 3 — Account reality check. When the authenticated account's isLive
    flag is known, it must agree with CTRADER_ENV. Catches a live account id
    pasted under a demo env (the dangerous direction), and warns on the
    harmless inverse.

assert_can_trade() runs all applicable layers and must be called immediately
before any order leaves the broker adapter.
"""

from __future__ import annotations

from trading_bot.config import CTraderEnv, Settings
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


class LiveTradingBlocked(RuntimeError):
    """Hard stop: an order would touch a live account in an unverified or
    misconfigured state. Never caught-and-ignored — it must halt the order."""


def assert_can_trade(settings: Settings, account_is_live: bool | None = None) -> None:
    """Raise LiveTradingBlocked if placing an order right now would be unsafe.

    Call immediately before submitting any order. Demo is always allowed
    (Layer 1 guarantees no live exposure). Live requires the explicit switch
    (Layer 2) and, when known, a matching account (Layer 3).

    `account_is_live` is the authenticated account's isLive flag if known,
    else None to skip Layer 3.
    """
    env = settings.ctrader_env

    # Layer 3: the account's reality must match the declared environment.
    if account_is_live is True and env == CTraderEnv.DEMO:
        raise LiveTradingBlocked(
            "CTRADER_ENV=demo but the authenticated account is LIVE. Refusing to "
            "trade. Check CTRADER_ACCOUNT_ID points at your demo account."
        )
    if account_is_live is False and env == CTraderEnv.LIVE:
        # Harmless (no real money), but a sign of misconfiguration — surface it.
        log.warning(
            "live_env_demo_account",
            message="CTRADER_ENV=live but the account is a demo account.",
        )

    # Layer 2: live trading needs the explicit switch.
    if env == CTraderEnv.LIVE and not settings.allow_live_trading:
        raise LiveTradingBlocked(
            "Live trading is DISABLED. CTRADER_ENV=live but ALLOW_LIVE_TRADING is "
            "false. Only set ALLOW_LIVE_TRADING=true after the strategy has been "
            "verified on the demo account."
        )

    # Demo always passes — Layer 1 (host isolation) guarantees no live exposure.
    log.info(
        "trading_allowed",
        env=env.value,
        allow_live=settings.allow_live_trading,
        account_is_live=account_is_live,
    )


def safety_posture(settings: Settings, account_is_live: bool | None = None) -> dict[str, object]:
    """Human-readable summary of the current safety state. Powers `tbot safety`."""
    env = settings.ctrader_env
    live_orders_possible = env == CTraderEnv.LIVE and settings.allow_live_trading
    return {
        "env": env.value,
        "host": settings.ctrader_host,
        "allow_live_trading": settings.allow_live_trading,
        "account_id": settings.ctrader_account_id,
        "account_is_live": account_is_live,
        "live_orders_possible": live_orders_possible,
    }
