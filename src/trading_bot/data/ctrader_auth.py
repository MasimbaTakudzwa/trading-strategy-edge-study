"""OAuth 2.0 login helper for cTrader Open API.

Runs the full authorization-code flow so the user never has to hand-copy
auth codes out of a browser:

  1. Build the auth URL (SDK Auth.getAuthUri)
  2. Open it in the default browser
  3. Catch the redirect on a temporary localhost:8080 server
  4. Exchange the auth code for access + refresh tokens (SDK Auth.getToken)
  5. Call GetAccountListByAccessToken to discover the ctidTraderAccountId(s)
     the token grants access to, flagging which are demo vs live

Access tokens last ~30 days. Re-run `tbot ctrader login` to mint fresh ones,
or use the refresh token programmatically (Auth.refreshToken).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from ctrader_open_api import Auth

from trading_bot.config import get_settings
from trading_bot.observability.logging import get_logger

log = get_logger(__name__)

REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8080
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}{REDIRECT_PATH}"

_SUCCESS_HTML = b"""<!doctype html><html><head><title>Authorised</title></head>
<body style="font-family:system-ui;text-align:center;padding-top:4rem">
<h1>Authorised</h1><p>Token issued. You can close this tab and return to the terminal.</p>
</body></html>"""

_FAILURE_HTML = b"""<!doctype html><html><head><title>Failed</title></head>
<body style="font-family:system-ui;text-align:center;padding-top:4rem">
<h1>Authorisation failed</h1><p>Check the terminal for details.</p>
</body></html>"""


@dataclass
class _Catcher:
    code: str | None = None
    error: str | None = None


@dataclass
class TraderAccount:
    ctid_trader_account_id: int
    is_live: bool
    trader_login: int


@dataclass
class LoginResult:
    access_token: str
    refresh_token: str
    expires_in: int
    accounts: list[TraderAccount] = field(default_factory=list)

    @property
    def demo_accounts(self) -> list[TraderAccount]:
        return [a for a in self.accounts if not a.is_live]


def _make_handler(catcher: _Catcher) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urlparse(self.path)
            if parsed.path != REDIRECT_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            if "code" in params:
                catcher.code = params["code"][0]
                body = _SUCCESS_HTML
            else:
                catcher.error = params.get("error", ["unknown_error"])[0]
                body = _FAILURE_HTML
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass  # silence stdlib's default stderr logging

    return Handler


def _wait_for_code(catcher: _Catcher, timeout: float) -> None:
    """Serve requests until the redirect arrives or we time out."""
    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), _make_handler(catcher))
    server.timeout = 1.0
    deadline = time.monotonic() + timeout
    try:
        while catcher.code is None and catcher.error is None:
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"No OAuth redirect within {timeout:.0f}s. "
                    f"Did you complete the authorisation in the browser?"
                )
            server.handle_request()
    finally:
        server.server_close()


def _fetch_accounts(access_token: str) -> list[TraderAccount]:
    """App-auth, then ask which accounts this token covers."""
    from ctrader_open_api.messages.OpenApiMessages_pb2 import (
        ProtoOAGetAccountListByAccessTokenReq,
        ProtoOAGetAccountListByAccessTokenRes,
    )

    from trading_bot.data.ctrader_protocol import CTraderProtocol

    protocol = CTraderProtocol.from_settings()
    protocol.connect_app_only()
    try:
        req = ProtoOAGetAccountListByAccessTokenReq()
        req.accessToken = access_token
        res = protocol.send(req)
        if not isinstance(res, ProtoOAGetAccountListByAccessTokenRes):
            raise RuntimeError(f"Unexpected response: {type(res).__name__}")
        return [
            TraderAccount(
                ctid_trader_account_id=acc.ctidTraderAccountId,
                is_live=acc.isLive,
                trader_login=acc.traderLogin,
            )
            for acc in res.ctidTraderAccount
        ]
    finally:
        protocol.close()


def run_login(timeout: float = 300.0, open_browser: bool = True) -> LoginResult:
    """Execute the full OAuth flow. Returns tokens + discovered accounts.

    `timeout` bounds how long we wait for the user to authorise in the browser.
    """
    settings = get_settings()
    if settings.ctrader_client_id == "replace-me":
        raise RuntimeError(
            "CTRADER_CLIENT_ID is not set in .env — fill in your Open API app "
            "credentials first."
        )

    auth = Auth(
        settings.ctrader_client_id,
        settings.ctrader_client_secret.get_secret_value(),
        REDIRECT_URI,
    )
    auth_uri = auth.getAuthUri()
    log.info("oauth_auth_uri_built", uri=auth_uri)

    catcher = _Catcher()
    if open_browser:
        import webbrowser

        webbrowser.open(auth_uri)

    _wait_for_code(catcher, timeout)

    if catcher.error:
        raise RuntimeError(f"OAuth authorisation failed: {catcher.error}")
    assert catcher.code is not None

    token_response = auth.getToken(catcher.code)
    # Success: {accessToken, refreshToken, expiresIn, tokenType}. Error responses
    # carry a populated errorCode.
    if token_response.get("errorCode"):
        raise RuntimeError(f"Token exchange failed: {token_response}")

    access_token = token_response["accessToken"]
    refresh_token = token_response["refreshToken"]
    expires_in = int(token_response.get("expiresIn", 0))

    accounts = _fetch_accounts(access_token)

    return LoginResult(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        accounts=accounts,
    )
