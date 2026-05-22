"""OANDA broker adapter. Implements the Broker protocol against OANDA v20.

The practice/live switch happens in config — this adapter just reads
the resolved REST URL and account ID. No code path here knows or cares
which environment it's running in.
"""

from __future__ import annotations

from trading_bot.config import get_settings
from trading_bot.execution.base import (
    AccountSnapshot,
    OrderRequest,
    OrderResult,
    Position,
)
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)


class OandaBroker:
    """OANDA v20 implementation of the Broker protocol.

    Stub — week-4 milestone fills in the oandapyV20 calls.
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._token = settings.oanda_api_token.get_secret_value()
        self._account_id = settings.oanda_account_id
        self._base_url = settings.oanda_rest_url
        self._env = settings.oanda_env.value
        log.info(
            "oanda_broker_initialised",
            env=self._env,
            base_url=self._base_url,
            account=self._account_id,
        )

    def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError("Wire up oandapyV20.endpoints.orders.OrderCreate in week-4")

    def cancel_order(self, broker_order_id: str) -> bool:
        raise NotImplementedError("Wire up oandapyV20.endpoints.orders.OrderCancel in week-4")

    def get_positions(self) -> list[Position]:
        raise NotImplementedError("Wire up oandapyV20.endpoints.positions.OpenPositions in week-4")

    def get_account(self) -> AccountSnapshot:
        raise NotImplementedError("Wire up oandapyV20.endpoints.accounts.AccountSummary in week-4")

    def close_all_positions(self) -> int:
        """Kill switch — flatten everything. Called by the risk gate on breach
        and by `tbot stop --force` from the CLI."""
        raise NotImplementedError("Implement in week-4. Must be idempotent and survive retries.")
