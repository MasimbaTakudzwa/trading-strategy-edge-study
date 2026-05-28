"""Tests for position reconciliation — the safeguard that catches drift
between the broker's reality and the bot's record."""

from __future__ import annotations

from trading_bot.oms.reconcile import reconcile_positions


def test_clean_when_matched() -> None:
    r = reconcile_positions({"XAUUSD": 0.25, "US 500": -1.0}, {"XAUUSD": 0.25, "US 500": -1.0})
    assert r.is_clean
    assert set(r.matched) == {"XAUUSD", "US 500"}


def test_detects_position_only_broker_has() -> None:
    r = reconcile_positions({"XAUUSD": 0.25}, {})
    assert not r.is_clean
    assert len(r.only_broker) == 1
    assert r.only_broker[0].instrument == "XAUUSD"


def test_detects_position_only_db_has() -> None:
    r = reconcile_positions({}, {"XAUUSD": 0.25})
    assert not r.is_clean
    assert len(r.only_db) == 1


def test_detects_size_mismatch() -> None:
    r = reconcile_positions({"XAUUSD": 0.25}, {"XAUUSD": 0.50})
    assert not r.is_clean
    assert len(r.mismatched) == 1
    assert r.mismatched[0].broker_units == 0.25
    assert r.mismatched[0].db_units == 0.50


def test_flat_represented_as_zero_or_absent() -> None:
    # Zero on one side, absent on the other → both flat → clean.
    r = reconcile_positions({"XAUUSD": 0.0}, {})
    assert r.is_clean


def test_tolerance_absorbs_float_noise() -> None:
    r = reconcile_positions({"XAUUSD": 0.2500000001}, {"XAUUSD": 0.25})
    assert r.is_clean
