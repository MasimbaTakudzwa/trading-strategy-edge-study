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
    strategy: Annotated[str, typer.Option("--strategy", "-s")],
    instrument: Annotated[str, typer.Option("--instrument", "-i")] = "EURUSD",
    granularity: Annotated[str, typer.Option("--granularity", "-g")] = "H1",
) -> None:
    """Run a strategy against stored historical candles."""
    console.print(f"[yellow]backtest stub[/yellow]: {strategy} on {instrument} {granularity}")
    log.info("backtest_requested", strategy=strategy, instrument=instrument)


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
    s = get_settings()
    if s.ctrader_env != CTraderEnv.LIVE:
        raise typer.Exit(
            f"CTRADER_ENV is {s.ctrader_env.value!r} — live trading requires 'live'."
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
    does on the demo account over time."""
    console.print(f"[yellow]report stub[/yellow]: env={env}, last {days}d — wire up in week-2")


if __name__ == "__main__":
    app()
