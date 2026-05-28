"""Trading bot CLI — `tbot <command>`.

Subcommands match the lifecycle phases:

  db init             schema migrations
  db status           show stored candle ranges
  fetch               backfill historical candles into Postgres
  ctrader symbols     list available instrument names → numeric IDs
  backtest            run a strategy against stored candles
  paper               run on cTrader demo account (env: CTRADER_ENV=demo)
  live                run on real cTrader account (prompts for confirmation)
  status              current positions, today's P&L
  reconcile           broker positions vs DB — must match
  report              performance summary, sliceable by env
  stop                graceful shutdown; --force to flatten positions
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import typer
from rich.console import Console

from trading_bot.config import CTraderEnv, get_settings
from trading_bot.observability.logging import configure_logging, get_logger

app = typer.Typer(
    name="tbot",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="FX trading bot (cTrader Open API + FP Markets).",
)
db_app = typer.Typer(no_args_is_help=True, help="Database operations.")
ctrader_app = typer.Typer(no_args_is_help=True, help="cTrader API helpers.")
app.add_typer(db_app, name="db")
app.add_typer(ctrader_app, name="ctrader")

console = Console()
log = get_logger("cli")


@app.callback()
def _root() -> None:
    """Configure logging once before any subcommand runs."""
    configure_logging()


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


@db_app.command("init")
def db_init() -> None:
    """Apply schema migrations (idempotent)."""
    from sqlalchemy import text

    from trading_bot.data.db import get_engine

    sql_path = "ops/sql/001_init.sql"
    with open(sql_path) as f:
        ddl = f.read()
    with get_engine().begin() as conn:
        for stmt in ddl.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(text(stmt))
    console.print(f"[green]Schema applied from {sql_path}[/green]")


@db_app.command("status")
def db_status() -> None:
    """Show stored candle ranges per (instrument, granularity)."""
    from rich.table import Table

    from trading_bot.data.candles import list_candle_ranges

    ranges = list_candle_ranges()
    if not ranges:
        console.print("[yellow]No candles stored yet. Run `tbot fetch` first.[/yellow]")
        return

    table = Table(title="Stored candles", show_lines=False)
    table.add_column("Instrument", style="cyan")
    table.add_column("Granularity", style="magenta")
    table.add_column("Count", justify="right")
    table.add_column("Earliest")
    table.add_column("Latest")
    for r in ranges:
        table.add_row(
            r.instrument,
            r.granularity,
            f"{r.count:,}",
            r.earliest.isoformat() if r.earliest else "-",
            r.latest.isoformat() if r.latest else "-",
        )
    console.print(table)


# ---------------------------------------------------------------------------
# ctrader helpers
# ---------------------------------------------------------------------------


@ctrader_app.command("login")
def ctrader_login(
    timeout: Annotated[
        int, typer.Option(help="Seconds to wait for browser authorisation.")
    ] = 300,
) -> None:
    """Run the OAuth flow and print the .env values to paste.

    Opens your browser to authorise the app against your cTrader account,
    catches the redirect locally, exchanges it for tokens, and discovers
    your account IDs. Requires CTRADER_CLIENT_ID + CTRADER_CLIENT_SECRET
    already set in .env.
    """
    from trading_bot.data.ctrader_auth import REDIRECT_URI, run_login

    console.print(
        f"Opening your browser to authorise. Redirect target: [cyan]{REDIRECT_URI}[/cyan]\n"
        f"(Make sure this exact URL is registered on your Open API app.)"
    )
    try:
        result = run_login(timeout=float(timeout))
    except Exception as e:  # noqa: BLE001 — surface any failure cleanly to the user
        console.print(f"[red]Login failed:[/red] {e}")
        raise typer.Exit(code=1) from e

    demos = result.demo_accounts
    console.print("\n[green]Authorised.[/green] Accounts on this token:")
    for acc in result.accounts:
        kind = "[red]LIVE[/red]" if acc.is_live else "[green]demo[/green]"
        console.print(f"  {kind}  ctidTraderAccountId=[bold]{acc.ctid_trader_account_id}[/bold]  login={acc.trader_login}")

    if len(demos) == 1:
        chosen = demos[0].ctid_trader_account_id
        note = "(your only demo account — use this)"
    elif len(demos) > 1:
        chosen = demos[0].ctid_trader_account_id
        note = "(first of several demos — pick the one matching account 5826141 etc.)"
    else:
        chosen = result.accounts[0].ctid_trader_account_id if result.accounts else 0
        note = "[yellow](no demo account found — double-check you authorised the demo)[/yellow]"

    console.print(f"\nPaste these into [cyan].env[/cyan] {note}:\n")
    console.print(f"[bold]CTRADER_ACCOUNT_ID[/bold]={chosen}")
    console.print(f"[bold]CTRADER_ACCESS_TOKEN[/bold]={result.access_token}")
    console.print(f"[bold]CTRADER_REFRESH_TOKEN[/bold]={result.refresh_token}")
    console.print(
        f"\nAccess token valid ~{result.expires_in // 86400} days. "
        f"Re-run [cyan]tbot ctrader login[/cyan] to refresh."
    )


@ctrader_app.command("symbols")
def ctrader_symbols(
    filter: Annotated[
        str, typer.Option(help="Substring filter on symbol name (case-insensitive).")
    ] = "",
) -> None:
    """List instruments available on the connected cTrader account.

    Broker naming varies — some use 'EURUSD', others 'EUR/USD'. Use this
    to find the exact string to pass to `tbot fetch`.
    """
    from rich.table import Table

    from trading_bot.data.ctrader_fetcher import CTraderFetcher
    from trading_bot.data.ctrader_protocol import CTraderProtocol

    needle = filter.lower()
    with console.status("Connecting to cTrader and loading symbols..."):
        with CTraderProtocol.from_settings() as protocol:
            fetcher = CTraderFetcher(protocol)
            symbols = fetcher.list_symbols()

    rows = sorted(
        (name, sid) for name, sid in symbols.items() if needle in name.lower()
    )
    table = Table(title=f"cTrader symbols ({len(rows)} of {len(symbols)})")
    table.add_column("Name", style="cyan")
    table.add_column("Symbol ID", justify="right", style="magenta")
    for name, sid in rows:
        table.add_row(name, str(sid))
    console.print(table)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@app.command()
def fetch(
    instrument: Annotated[str, typer.Argument(help="e.g. EURUSD (run `tbot ctrader symbols` to find names)")],
    granularity: Annotated[str, typer.Argument(help="M1, M5, M15, M30, H1, H4, D1, W1")] = "H1",
    since: Annotated[
        str, typer.Option(help="ISO date. Ignored if --resume is set.")
    ] = "2020-01-01",
    resume: Annotated[
        bool,
        typer.Option(
            "--resume",
            help="Resume from the latest stored candle for this series. Falls back to --since if none.",
        ),
    ] = False,
) -> None:
    """Backfill historical candles into Postgres.

    Idempotent: re-runs over the same window won't duplicate rows. With
    --resume, only fetches bars newer than what's already stored.
    """
    from trading_bot.data.candles import latest_candle_ts
    from trading_bot.data.ctrader_fetcher import GRANULARITY_MAP, CTraderFetcher
    from trading_bot.data.ctrader_protocol import CTraderProtocol

    if granularity not in GRANULARITY_MAP:
        raise typer.Exit(
            f"Unknown granularity {granularity!r}. "
            f"Valid: {', '.join(sorted(GRANULARITY_MAP))}"
        )

    if resume:
        latest = latest_candle_ts(instrument, granularity)
        if latest is not None:
            _, minutes = GRANULARITY_MAP[granularity]
            start = latest + timedelta(minutes=minutes)
            console.print(
                f"Resuming from latest stored candle: {latest.isoformat()} "
                f"(next bar {start.isoformat()})"
            )
        else:
            start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
            console.print(f"No prior data, starting from {start.isoformat()}")
    else:
        start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)

    with console.status(f"Fetching {instrument} {granularity} from {start.isoformat()}..."):
        with CTraderProtocol.from_settings() as protocol:
            fetcher = CTraderFetcher(protocol)
            n = fetcher.backfill(instrument, granularity, start)
    console.print(f"Upserted [bold]{n:,}[/bold] candles for {instrument} {granularity}")


# ---------------------------------------------------------------------------
# backtest / paper / live
# ---------------------------------------------------------------------------


@app.command()
def backtest(
    strategy: Annotated[str, typer.Option("--strategy", "-s", help="donchian | meanrev")] = "donchian",
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EURUSD",
    granularity: Annotated[str, typer.Option("--granularity", "-g")] = "H1",
    capital: Annotated[float, typer.Option(help="Starting cash.")] = 1000.0,
    entry_period: Annotated[int, typer.Option(help="[donchian] entry channel length.")] = 20,
    exit_period: Annotated[int, typer.Option(help="[donchian] exit channel length.")] = 10,
    trend_filter: Annotated[
        int, typer.Option(help="[donchian] trend-filter SMA length (0 = off).")
    ] = 0,
    period: Annotated[int, typer.Option(help="[meanrev] Bollinger lookback.")] = 20,
    num_std: Annotated[float, typer.Option(help="[meanrev] band width in std devs.")] = 2.0,
    stop_loss: Annotated[
        float, typer.Option(help="Hard stop as a fraction, e.g. 0.02 = 2% (0 = off).")
    ] = 0.0,
    fees: Annotated[float, typer.Option(help="Commission fraction per side.")] = 0.00003,
    slippage: Annotated[float, typer.Option(help="Slippage fraction per side.")] = 0.00002,
) -> None:
    """Run a strategy against stored historical candles."""
    from rich.table import Table

    from trading_bot.backtest.runner import run_backtest
    from trading_bot.data.candles import load_candles
    from trading_bot.strategies.donchian import DonchianParams, DonchianStrategy
    from trading_bot.strategies.mean_reversion import BollingerParams, MeanReversionStrategy

    df = load_candles(instrument, granularity)
    if df.empty:
        raise typer.Exit(
            f"No stored candles for {instrument} {granularity}. "
            f"Run `tbot fetch {instrument} {granularity}` first."
        )

    if strategy == "donchian":
        filter_period = trend_filter if trend_filter > 0 else None
        strat: object = DonchianStrategy(
            DonchianParams(
                entry_period=entry_period,
                exit_period=exit_period,
                trend_filter_period=filter_period,
            )
        )
        desc = f"entry={entry_period}/exit={exit_period}, trend_filter={filter_period or 'off'}"
    elif strategy in ("meanrev", "mean_reversion"):
        strat = MeanReversionStrategy(BollingerParams(period=period, num_std=num_std))
        desc = f"period={period}, num_std={num_std}"
    else:
        raise typer.Exit(f"Unknown strategy {strategy!r}. Available: donchian, meanrev")

    sl = stop_loss if stop_loss > 0 else None
    console.print(
        f"Backtesting [bold]{strategy}[/bold] on {instrument} {granularity}: "
        f"{len(df):,} bars, {df.index[0].date()} → {df.index[-1].date()}, "
        f"{desc}, stop_loss={sl or 'off'}, capital={capital:,.0f}"
    )

    result = run_backtest(
        df,
        strat,  # type: ignore[arg-type]
        init_cash=capital,
        fees=fees,
        slippage=slippage,
        granularity=granularity,
        stop_loss=sl,
    )

    # Pull the headline metrics from vectorbt's stats Series.
    s = result.stats
    table = Table(title=f"{strategy} backtest — {instrument} {granularity}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    def _row(label: str, key: str, fmt: str = "{}") -> None:
        if key in s.index:
            table.add_row(label, fmt.format(s[key]))

    _row("Total return", "Total Return [%]", "{:.2f}%")
    _row("Benchmark (buy & hold)", "Benchmark Return [%]", "{:.2f}%")
    _row("Max drawdown", "Max Drawdown [%]", "{:.2f}%")
    _row("Sharpe ratio", "Sharpe Ratio", "{:.2f}")
    _row("Sortino ratio", "Sortino Ratio", "{:.2f}")
    _row("Win rate", "Win Rate [%]", "{:.1f}%")
    _row("Total trades", "Total Trades", "{:.0f}")
    _row("Profit factor", "Profit Factor", "{:.2f}")
    _row("Final value", "End Value", "{:,.2f}")
    console.print(table)

    log.info(
        "backtest_requested",
        strategy=strategy,
        instrument=instrument,
        granularity=granularity,
        bars=len(df),
    )


@app.command()
def paper(
    strategy: Annotated[str, typer.Option("--strategy", "-s")],
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EURUSD",
) -> None:
    """Run on cTrader demo account. Logs everything to DB tagged env='practice'."""
    s = get_settings()
    if s.ctrader_env != CTraderEnv.DEMO:
        raise typer.Exit(
            f"CTRADER_ENV is {s.ctrader_env.value!r} — paper trading requires 'demo'."
        )
    console.print(
        f"[green]paper trading stub[/green]: {strategy} on {instrument} (env=demo)"
    )
    log.info("paper_requested", strategy=strategy, instrument=instrument)


@app.command()
def live(
    strategy: Annotated[str, typer.Option("--strategy", "-s")],
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EURUSD",
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation prompt.")] = False,
) -> None:
    """Run on a LIVE cTrader account. Requires explicit confirmation."""
    from trading_bot.risk.safety import LiveTradingBlocked, assert_can_trade

    s = get_settings()
    if s.ctrader_env != CTraderEnv.LIVE:
        raise typer.Exit(
            f"CTRADER_ENV is {s.ctrader_env.value!r} — live trading requires 'live'."
        )
    # Safety gate — refuses unless ALLOW_LIVE_TRADING=true (and account matches).
    try:
        assert_can_trade(s)
    except LiveTradingBlocked as e:
        console.print(f"[red]Blocked:[/red] {e}")
        raise typer.Exit(code=1) from e
    if not yes:
        confirm = typer.confirm(
            f"About to trade LIVE money: {strategy} on {instrument}. Continue?",
            default=False,
        )
        if not confirm:
            raise typer.Exit("Aborted.")
    console.print(f"[red]LIVE trading stub[/red]: {strategy} on {instrument}")
    log.warning("live_requested", strategy=strategy, instrument=instrument)


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


@app.command()
def safety() -> None:
    """Show the current trading-safety posture — can the bot place live orders?"""
    from rich.table import Table

    from trading_bot.risk.safety import safety_posture

    posture = safety_posture(get_settings())
    table = Table(title="Trading safety posture")
    table.add_column("Check", style="cyan")
    table.add_column("Value")
    table.add_row("Environment", str(posture["env"]))
    table.add_row("Host", str(posture["host"]))
    table.add_row("Account ID", str(posture["account_id"]))
    table.add_row("ALLOW_LIVE_TRADING", str(posture["allow_live_trading"]))

    live_possible = posture["live_orders_possible"]
    verdict = (
        "[red]YES — live orders CAN be placed[/red]"
        if live_possible
        else "[green]NO — live orders are blocked[/green]"
    )
    table.add_row("Live orders possible?", verdict)
    console.print(table)

    if not live_possible:
        console.print(
            "\n[green]Safe.[/green] The bot cannot place live orders in this "
            "configuration. To enable live trading you must BOTH set "
            "CTRADER_ENV=live AND ALLOW_LIVE_TRADING=true."
        )


@app.command()
def status() -> None:
    """Show open positions and today's P&L."""
    console.print("[yellow]status stub[/yellow]: wire up in week-4")


@app.command()
def reconcile() -> None:
    """Compare broker positions to DB. Must match."""
    console.print("[yellow]reconcile stub[/yellow]: wire up in week-4")


@app.command()
def report(
    env: Annotated[str, typer.Option(help="practice | live | backtest")] = "practice",
    days: Annotated[int, typer.Option(help="lookback window")] = 30,
) -> None:
    """Performance summary sliceable by env. Use this to track how the bot
    does on the demo account over time."""
    console.print(f"[yellow]report stub[/yellow]: env={env}, last {days}d — wire up in week-2")


if __name__ == "__main__":
    app()
