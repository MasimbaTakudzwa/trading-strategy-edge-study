"""cTrader Open API broker adapter. Implements the Broker protocol.

Same demo/live switch as the rest of the cTrader stack — the underlying
protocol decides which host based on config. Strategy and risk code never
import this module directly; they go through the Broker protocol.

Stubbed for now. Filled in at week-4 alongside the OMS.
"""

from __future__ import annotations

from trading_bot.config import get_settings
from trading_bot.data.ctrader_protocol import CTraderProtocol
from trading_bot.execution.base import (
    AccountSnapshot,
    OrderRequest,
    OrderResult,
    Position,
)
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


class CTraderBroker:
    """cTrader Open API implementation of the Broker protocol."""

    def __init__(self, protocol: CTraderProtocol | None = None) -> None:
        self._protocol = protocol or CTraderProtocol.from_settings()
        settings = get_settings()
        self._env = settings.ctrader_env.value
        self._account_id = settings.ctrader_account_id
        # Populated in week-4 once we fetch trader info; gates Layer 3.
        self._account_is_live: bool | None = None
        log.info(
            "ctrader_broker_initialised",
            env=self._env,
            account_id=self._account_id,
        )

    def _guard(self) -> None:
        """Run all safety gates. Raises LiveTradingBlocked if unsafe. Called
        before every order-placing operation — never bypass."""
        from trading_bot.risk.safety import assert_can_trade

        assert_can_trade(get_settings(), account_is_live=self._account_is_live)

    def place_order(self, request: OrderRequest) -> OrderResult:
        self._guard()
        raise NotImplementedError(
            "Wire up ProtoOANewOrderReq in week-4. "
            "Map our Side → ProtoOATradeSide, OrderType → ProtoOAOrderType."
        )

    def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError("Wire up ProtoOACancelOrderReq in week-4.")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError(
            "Wire up ProtoOAReconcileReq in week-4. "
            "Response has both `position` and `order` lists."
        )

    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError("Wire up ProtoOATraderReq in week-4.")

    def close_all_positions(self) -> int:
        """Kill switch — flatten everything. Called by the risk gate on
        breach and by `tbot stop --force`. Must be idempotent.

        Note: this is a risk-reducing action, so it is intentionally NOT
        gated by _guard() — we must always be able to flatten, even if live
        trading is otherwise disabled.
        """
        raise NotImplementedError(
            "Implement in week-4 using ProtoOAClosePositionReq per open position."
        )
