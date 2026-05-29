"""Generate the figures for docs/thesis/thesis.md.

Run from the repo root:  uv run python docs/thesis/make_figures.py

The bar charts (Fig 1, Fig 2) are driven by the recorded backtest scorecard.
The equity curves (Fig 3) are regenerated from stored candles via the project's
own run_backtest, so they require the database to be up with XAUUSD/EURUSD D1
data loaded. Each figure is isolated so one failure can't block the others.
"""

from __future__ import annotations

import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

FIGDIR = os.path.join(os.path.dirname(__file__), "figures")
os.makedirs(FIGDIR, exist_ok=True)

# Recorded scorecard: (label, strategy return %, buy-hold %, Sharpe, profit factor)
TESTS = [
    ("Gold D1\ntrend · 22yr", 28.55, 983.61, 0.18, 1.17),
    ("Gold H1\ntrend · 8yr", 25.96, 244.46, 0.33, 1.08),
    ("EURUSD H1\ntrend · 8yr", -11.25, -3.13, -0.24, 0.93),
    ("EURUSD H1\nmean-rev · 8yr", 5.89, -3.13, 0.17, 1.02),
    ("EURUSD D1\ntrend · 22yr", -12.68, -7.45, -0.06, 0.88),
    ("EURUSD D1\nmean-rev · 22yr", -3.65, -7.45, 0.01, 0.98),
]
labels = [t[0] for t in TESTS]
strat = np.array([t[1] for t in TESTS])
bench = np.array([t[2] for t in TESTS])
sharpe = np.array([t[3] for t in TESTS])
pf = np.array([t[4] for t in TESTS])
x = np.arange(len(TESTS))

STRAT_C = "#c0392b"
BENCH_C = "#2c3e50"
WARN_C = "#e67e22"
GOOD_C = "#27ae60"


def _annotate(ax, xs, vals, fmt="{:.0f}%"):
    for xi, v in zip(xs, vals):
        ax.annotate(
            fmt.format(v), (xi, v), ha="center",
            va="bottom" if v >= 0 else "top", fontsize=7,
        )


# -- Figure 1: strategy return vs buy-and-hold -------------------------------
try:
    fig, ax = plt.subplots(figsize=(11, 5.5))
    w = 0.4
    ax.bar(x - w / 2, strat, w, label="Strategy", color=STRAT_C)
    ax.bar(x + w / 2, bench, w, label="Buy & hold", color=BENCH_C)
    ax.set_yscale("symlog", linthresh=10)
    ax.axhline(0, color="black", lw=0.8)
    ax.set_ylabel("Total return (%, symlog scale)")
    ax.set_title(
        "Figure 1 — Strategy total return vs. buy-and-hold\n"
        "Every active result trails simply holding the asset; gold buy-hold (+984%) dwarfs all."
    )
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.legend(loc="upper right")
    _annotate(ax, x - w / 2, strat)
    _annotate(ax, x + w / 2, bench)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGDIR, "fig1_return_vs_benchmark.png"), dpi=140)
    plt.close(fig)
    print("fig1 (return vs benchmark): OK")
except Exception as e:  # noqa: BLE001
    print(f"fig1 SKIPPED: {type(e).__name__}: {e}")


# -- Figure 2: risk-adjusted quality (Sharpe + profit factor) ----------------
try:
    fig, (axS, axP) = plt.subplots(1, 2, figsize=(12, 5))

    axS.bar(x, sharpe, color=[STRAT_C if v < 1.0 else GOOD_C for v in sharpe])
    axS.axhline(1.0, color=GOOD_C, ls="--", lw=1.2)
    axS.text(len(x) - 0.5, 1.02, "≈ min 'tradeable' (1.0)", ha="right", color=GOOD_C, fontsize=8)
    axS.axhline(0, color="black", lw=0.8)
    axS.set_title("Figure 2a — Sharpe ratio (annualised)")
    axS.set_xticks(x)
    axS.set_xticklabels(labels, fontsize=7)
    _annotate(axS, x, sharpe, fmt="{:.2f}")

    axP.bar(x, pf, color=[GOOD_C if v >= 1.3 else (STRAT_C if v < 1.0 else WARN_C) for v in pf])
    axP.axhline(1.0, color="black", ls="--", lw=1.0)
    axP.text(len(x) - 0.5, 1.005, "breakeven (1.0)", ha="right", fontsize=8)
    axP.axhline(1.3, color=GOOD_C, ls="--", lw=1.0)
    axP.text(len(x) - 0.5, 1.305, "viable (1.3)", ha="right", color=GOOD_C, fontsize=8)
    axP.set_ylim(0.8, 1.4)
    axP.set_title("Figure 2b — Profit factor")
    axP.set_xticks(x)
    axP.set_xticklabels(labels, fontsize=7)
    _annotate(axP, x, pf, fmt="{:.2f}")

    fig.suptitle("Figure 2 — Risk-adjusted quality: nothing clears the bar", fontsize=12)
    fig.tight_layout()
    fig.savefig(os.path.join(FIGDIR, "fig2_risk_metrics.png"), dpi=140)
    plt.close(fig)
    print("fig2 (risk metrics): OK")
except Exception as e:  # noqa: BLE001
    print(f"fig2 SKIPPED: {type(e).__name__}: {e}")


# -- Figure 3: equity curves (regenerated from stored candles) ---------------
try:
    from trading_bot.backtest.runner import run_backtest
    from trading_bot.data.candles import load_candles
    from trading_bot.strategies.donchian import DonchianParams, DonchianStrategy
    from trading_bot.strategies.mean_reversion import BollingerParams, MeanReversionStrategy

    def equity(instrument: str, strat_obj: object):
        df = load_candles(instrument, "D1")
        res = run_backtest(df, strat_obj, init_cash=1000.0, granularity="D1")  # type: ignore[arg-type]
        buy_hold = 1000.0 * df["close"] / df["close"].iloc[0]
        return res.portfolio.value(), buy_hold

    fig, (axg, axe) = plt.subplots(1, 2, figsize=(13, 5))

    gv, gbh = equity("XAUUSD", DonchianStrategy(DonchianParams(entry_period=55, exit_period=20)))
    axg.plot(gbh.index, gbh.values, label="Buy & hold gold", color=BENCH_C, lw=1.6)
    axg.plot(gv.index, gv.values, label="Trend strategy", color=STRAT_C, lw=1.6)
    axg.set_yscale("log")
    axg.set_title("Figure 3a — Gold (XAUUSD) daily, 2004–2026\nstrategy vs. buy-and-hold (log scale)")
    axg.set_ylabel("Account value from $1,000 (log)")
    axg.legend(loc="upper left")

    et, ebh = equity("EURUSD", DonchianStrategy(DonchianParams(entry_period=55, exit_period=20)))
    em, _ = equity("EURUSD", MeanReversionStrategy(BollingerParams(period=20, num_std=2.0)))
    axe.plot(ebh.index, ebh.values, label="Buy & hold", color=BENCH_C, lw=1.6)
    axe.plot(et.index, et.values, label="Trend", color=STRAT_C, lw=1.3)
    axe.plot(em.index, em.values, label="Mean-reversion", color=WARN_C, lw=1.3)
    axe.axhline(1000, color="gray", ls=":", lw=0.8)
    axe.set_title("Figure 3b — EUR/USD daily, 2004–2026\nboth strategies vs. (flat) buy-and-hold")
    axe.set_ylabel("Account value from $1,000")
    axe.legend(loc="upper left")

    fig.tight_layout()
    fig.savefig(os.path.join(FIGDIR, "fig3_equity_curves.png"), dpi=140)
    plt.close(fig)
    print("fig3 (equity curves): OK")
except Exception as e:  # noqa: BLE001
    print(f"fig3 SKIPPED ({type(e).__name__}: {e}) — bar charts still generated")

print("figures written to", FIGDIR)
