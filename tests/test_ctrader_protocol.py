"""Tests for the cTrader protocol's transient-failure retry.

We never touch the real Twisted reactor: the module-level `_send_in_reactor`
(and `Protobuf`) are stubbed so `_send_once`'s transport call is fully
controlled. Then we assert the retry policy — transient transport errors are
retried up to the cap, but a logical `CTraderError` is not.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from trading_bot.data import ctrader_protocol as cp
from trading_bot.data.ctrader_protocol import CTraderError, CTraderProtocol, ReactorTimeout


def _protocol() -> CTraderProtocol:
    """A protocol with zero retry delay and a pretend-connected client."""
    p = CTraderProtocol(
        host="h",
        port=1,
        client_id="c",
        client_secret="s",
        account_id=1,
        access_token="t",
        max_send_attempts=3,
        retry_base_delay=0.0,
        retry_max_delay=0.0,
    )
    p._client = object()  # bypass the "Not connected" guard
    return p


def _identity_protobuf() -> types.SimpleNamespace:
    return types.SimpleNamespace(extract=lambda envelope: envelope)


def test_transient_error_is_retried_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_send(client: Any, message: Any) -> Any:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ReactorTimeout("timed out")
        return "ENVELOPE"

    monkeypatch.setattr(cp, "_send_in_reactor", fake_send)
    monkeypatch.setattr(cp, "Protobuf", _identity_protobuf())

    result = _protocol()._request("REQ")

    assert result == "ENVELOPE"
    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


def test_transient_error_exhausts_attempts_and_reraises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def fake_send(client: Any, message: Any) -> Any:
        calls["n"] += 1
        raise ReactorTimeout("still down")

    monkeypatch.setattr(cp, "_send_in_reactor", fake_send)
    monkeypatch.setattr(cp, "Protobuf", _identity_protobuf())

    with pytest.raises(ReactorTimeout):
        _protocol()._request("REQ")
    assert calls["n"] == 3  # capped at max_send_attempts, then reraised


def test_logical_ctrader_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def fake_send(client: Any, message: Any) -> Any:
        calls["n"] += 1
        raise CTraderError("OA_MARKET_CLOSED", "market closed")

    monkeypatch.setattr(cp, "_send_in_reactor", fake_send)

    with pytest.raises(CTraderError):
        _protocol()._request("REQ")
    assert calls["n"] == 1  # deterministic rejection — not retried


def test_not_connected_raises_without_sending() -> None:
    p = CTraderProtocol(
        host="h", port=1, client_id="c", client_secret="s", account_id=1, access_token="t"
    )
    with pytest.raises(RuntimeError, match="Not connected"):
        p._request("REQ")
