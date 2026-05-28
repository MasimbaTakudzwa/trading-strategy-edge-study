"""Sync facade over the Twisted-based cTrader Open API SDK.

The official SDK (`ctrader_open_api`) is callback-based on top of the
Twisted reactor. We use crochet to run the reactor in a background thread
and expose blocking sync methods, so the rest of the bot (strategies,
fetchers, CLI) stays sync — no asyncio sprawl, no Twisted in their imports.

Auth is a three-step ceremony:
    1. TLS connect to the demo/live host (port 5035)
    2. ProtoOAApplicationAuthReq — identifies the registered Open API app
    3. ProtoOAAccountAuthReq    — links the session to a specific account
       via its ctidTraderAccountId + access_token

Only after step 3 can data and trading requests succeed.
"""

from __future__ import annotations

from typing import Any

from crochet import run_in_reactor
from crochet import setup as crochet_setup
from crochet import wait_for
from ctrader_open_api import Client, TcpProtocol
from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import ProtoErrorRes
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAAccountAuthReq,
    ProtoOAApplicationAuthReq,
    ProtoOAErrorRes,
)
from twisted.internet.defer import Deferred

from trading_bot.observability.logging import get_logger

log = get_logger(__name__)

# Initialise the Twisted reactor in a background thread, exactly once.
# Idempotent — safe to call from multiple imports.
crochet_setup()


DEFAULT_SEND_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 30.0


class CTraderError(RuntimeError):
    """Raised when cTrader returns an error response."""

    def __init__(self, code: str, description: str) -> None:
        super().__init__(f"cTrader error {code}: {description}")
        self.code = code
        self.description = description


@run_in_reactor
def _start_and_connect(client: Client) -> Deferred:
    """Start the TLS connection. Returns a Deferred that fires (with None)
    when the SDK's 'connected' callback fires, or errbacks on disconnect
    before connection completes."""
    d: Deferred = Deferred()

    def on_connected(_: Client) -> None:
        if not d.called:
            d.callback(None)

    def on_disconnected(_: Client, reason: Any) -> None:
        if not d.called:
            d.errback(RuntimeError(f"Disconnected before connect completed: {reason}"))

    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.startService()
    return d


@wait_for(timeout=DEFAULT_SEND_TIMEOUT)
def _send_in_reactor(client: Client, message: Any) -> Deferred:
    """Send a protobuf message and return the response (sync via crochet)."""
    return client.send(message)


@run_in_reactor
def _stop_in_reactor(client: Client) -> None:
    client.stopService()


def _check_for_error(response: Any) -> Any:
    """Raise CTraderError if the response is an API error envelope."""
    if isinstance(response, (ProtoOAErrorRes, ProtoErrorRes)):
        raise CTraderError(response.errorCode, response.description)
    return response


class CTraderProtocol:
    """Connection + auth + send/recv for the cTrader Open API.

    Use as a context manager for clean shutdown:

        with CTraderProtocol.from_settings() as p:
            response = p.send(some_request)
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        client_secret: str,
        account_id: int,
        access_token: str,
    ) -> None:
        self._host = host
        self._port = port
        self._client_id = client_id
        self._client_secret = client_secret
        self._account_id = account_id
        self._access_token = access_token
        self._client: Client | None = None
        self._app_authed = False
        self._account_authed = False

    @classmethod
    def from_settings(cls) -> "CTraderProtocol":
        from trading_bot.config import get_settings

        s = get_settings()
        return cls(
            host=s.ctrader_host,
            port=s.ctrader_port,
            client_id=s.ctrader_client_id,
            client_secret=s.ctrader_client_secret.get_secret_value(),
            account_id=s.ctrader_account_id,
            access_token=s.ctrader_access_token.get_secret_value(),
        )

    @property
    def account_id(self) -> int:
        return self._account_id

    @property
    def is_authenticated(self) -> bool:
        return self._account_authed

    def _ensure_app_authed(self, timeout: float) -> None:
        """Open the TLS connection and authenticate the application. Idempotent."""
        if self._client is None:
            self._client = Client(self._host, self._port, TcpProtocol)
            log.info("ctrader_connecting", host=self._host, port=self._port)
            evt = _start_and_connect(self._client)
            evt.wait(timeout)
            log.info("ctrader_connected")

        if not self._app_authed:
            app_req = ProtoOAApplicationAuthReq()
            app_req.clientId = self._client_id
            app_req.clientSecret = self._client_secret
            _check_for_error(_send_in_reactor(self._client, app_req))
            self._app_authed = True
            log.info("ctrader_app_authenticated")

    def connect_app_only(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """Connect + app auth, WITHOUT account auth.

        Used by the OAuth login flow: GetAccountListByAccessToken needs app
        auth but runs *before* we know which account to authenticate.
        """
        self._ensure_app_authed(timeout)

    def connect(self, timeout: float = DEFAULT_CONNECT_TIMEOUT) -> None:
        """Connect, authenticate the app, then the account. Blocks until ready."""
        if self._account_authed:
            return

        self._ensure_app_authed(timeout)

        acc_req = ProtoOAAccountAuthReq()
        acc_req.ctidTraderAccountId = self._account_id
        acc_req.accessToken = self._access_token
        _check_for_error(_send_in_reactor(self._client, acc_req))
        log.info("ctrader_account_authenticated", account_id=self._account_id)

        self._account_authed = True

    def send(self, message: Any) -> Any:
        """Send a request and return the (unwrapped) response. Raises on error.

        Requires at least app auth (connect_app_only or connect). Account-scoped
        requests will still fail server-side if the account isn't authenticated.
        """
        if self._client is None or not self._app_authed:
            raise RuntimeError("Call connect() or connect_app_only() before send()")
        return _check_for_error(_send_in_reactor(self._client, message))

    def close(self) -> None:
        if self._client is not None:
            _stop_in_reactor(self._client)
            self._client = None
            self._app_authed = False
            self._account_authed = False
            log.info("ctrader_closed")

    def __enter__(self) -> "CTraderProtocol":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
