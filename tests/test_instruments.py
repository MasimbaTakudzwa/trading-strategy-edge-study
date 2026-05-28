"""Tests for volume conversion and the small-account min-size guard — the
code that stands between the bot and a position it can't size safely."""

from __future__ import annotations

import pytest

from trading_bot.execution.instruments import (
    SymbolSpec,
    api_volume_to_units,
    fit_order_size,
    units_to_api_volume,
)

# Real specs pulled from FP Markets.
EURUSD = SymbolSpec(1, "EURUSD", digits=5, pip_position=4, min_volume=100_000, step_volume=100_000, max_volume=1_000_000_000, lot_size=10_000_000)
GOLD = SymbolSpec(41, "XAUUSD", digits=2, pip_position=2, min_volume=100, step_volume=100, max_volume=1_000_000, lot_size=10_000)


# ---------------------------------------------------------------------------
# Volume conversion (the ×100 rule)
# ---------------------------------------------------------------------------


def test_micro_lot_converts_to_min_volume() -> None:
    # 1,000 units of EURUSD = a 0.01 lot = API volume 100,000 (the minimum).
    assert units_to_api_volume(1000, EURUSD) == 100_000


def test_volume_floors_to_step() -> None:
    # 2,500 units → 0.025 lot → floored to 0.02 lot (200,000), not rounded up.
    assert units_to_api_volume(2500, EURUSD) == 200_000


def test_sub_step_size_floors_to_zero() -> None:
    # Half an ounce of gold is below the 1 oz step → 0 (not tradeable as-is).
    assert units_to_api_volume(0.5, GOLD) == 0


def test_api_volume_to_units_roundtrip() -> None:
    assert api_volume_to_units(100_000) == 1000  # EURUSD micro lot
    assert api_volume_to_units(100) == 1.0  # 1 oz gold


# ---------------------------------------------------------------------------
# Min-size guard — the small-account safeguard
# ---------------------------------------------------------------------------


def test_gold_refused_on_small_account() -> None:
    """1 oz gold with a $150 stop risks far more than 0.5% of $1,000."""
    order = fit_order_size(
        desired_units=0.03,
        spec=GOLD,
        stop_distance=150.0,
        value_per_point=1.0,
        equity=1000.0,
        max_risk_fraction=0.005,
    )
    assert not order.tradeable
    assert order.units == 0
    assert "too small" in (order.reason or "").lower()


def test_eurusd_micro_lot_fits_small_account() -> None:
    """1,000 units EURUSD with a 50-pip (0.0050) stop risks exactly $5 = 0.5%."""
    order = fit_order_size(
        desired_units=1000,
        spec=EURUSD,
        stop_distance=0.0050,
        value_per_point=1.0,
        equity=1000.0,
        max_risk_fraction=0.005,
    )
    assert order.tradeable
    assert order.units == 1000
    assert order.api_volume == 100_000
    assert order.risk_amount == pytest.approx(5.0)


def test_desired_below_min_bumped_to_min_when_affordable() -> None:
    order = fit_order_size(
        desired_units=300,
        spec=EURUSD,
        stop_distance=0.0050,
        value_per_point=1.0,
        equity=10_000.0,  # budget $50, min risk $5 → affordable
        max_risk_fraction=0.005,
    )
    assert order.tradeable
    assert order.units == 1000  # bumped up to the minimum


def test_larger_desired_floors_to_step() -> None:
    order = fit_order_size(
        desired_units=2500,
        spec=EURUSD,
        stop_distance=0.0050,
        value_per_point=1.0,
        equity=100_000.0,
        max_risk_fraction=0.005,
    )
    assert order.tradeable
    assert order.units == 2000  # 2,500 floored to a 0.02-lot step
