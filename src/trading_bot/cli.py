"""Trading bot CLI — `tbot <command>`.

Subcommands match the lifecycle phases:

  db init             schema migrations
  fetch               backfill historical candles into Postgres
  backtest            run a strategy against stored candles
  paper               run on OANDA practice account (env: OANDA_ENV=practice)
  live                run on real OANDA account (prompts for confirmation)
  status              current positions, today's P&L
  reconcile           broker positions vs DB — must match
  report              performance summary, sliceable by env
  stop                graceful shutdown; --force to flatten positions
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

import typer
from rich.console import Console

from trading_bot.config import OandaEnv, get_settings
from trading_bot.observability.logging import configure_logging, get_logger

app = typer.Typer(
    name="tbot",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
    help="FX/futures trading bot.",
)
db_app = typer.Typer(no_args_is_help=True, help="Database operations.")
app.add_typer(db_app, name="db")

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


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@app.command()
def fetch(
    instrument: Annotated[str, typer.Argument(help="e.g. EUR_USD")],
    granularity: Annotated[str, typer.Argument(help="M1, M5, H1, H4, D")] = "H1",
    since: Annotated[str, typer.Option(help="ISO date, e.g. 2020-01-01")] = "2020-01-01",
) -> None:
    """Backfill historical candles into Postgres."""
    from trading_bot.data.oanda_fetcher import OandaFetcher

    start = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    fetcher = OandaFetcher()
    n = fetcher.backfill(instrument, granularity, start)
    console.print(f"Fetched [bold]{n}[/bold] candles for {instrument} {granularity}")


# ---------------------------------------------------------------------------
# backtest / paper / live
# ---------------------------------------------------------------------------


@app.command()
def backtest(
    strategy: Annotated[str, typer.Option("--strategy", "-s")],
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EUR_USD",
    granularity: Annotated[str, typer.Option("--granularity", "-g")] = "H1",
) -> None:
    """Run a strategy against stored historical candles."""
    console.print(f"[yellow]backtest stub[/yellow]: {strategy} on {instrument} {granularity}")
    log.info("backtest_requested", strategy=strategy, instrument=instrument)


@app.command()
def paper(
    strategy: Annotated[str, typer.Option("--strategy", "-s")],
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EUR_USD",
) -> None:
    """Run on OANDA practice account. Logs everything to DB tagged env='practice'."""
    s = get_settings()
    if s.oanda_env != OandaEnv.PRACTICE:
        raise typer.Exit(
            f"OANDA_ENV is '{s.oanda_env.value}' — paper trading requires 'practice'."
        )
    console.print(
        f"[green]paper trading stub[/green]: {strategy} on {instrument} (env=practice)"
    )
    log.info("paper_requested", strategy=strategy, instrument=instrument)


@app.command()
def live(
    strategy: Annotated[str, typer.Option("--strategy", "-s")],
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EUR_USD",
    yes: Annotated[bool, typer.Option("--yes", help="Skip confirmation prompt.")] = False,
) -> None:
    """Run on a LIVE OANDA account. Requires explicit confirmation."""
    s = get_settings()
    if s.oanda_env != OandaEnv.LIVE:
        raise typer.Exit(
            f"OANDA_ENV is '{s.oanda_env.value}' — live trading requires 'live'."
        )
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
    does on the practice account over time."""
    console.print(f"[yellow]report stub[/yellow]: env={env}, last {days}d — wire up in week-2")


if __name__ == "__main__":
    app()
