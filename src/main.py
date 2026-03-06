import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from src.config.settings import get_settings
from src.utils.logger import setup_logger
from src.storage.db import init_db
from src.storage.trade_log import TradeLog
from src.adaptive.goal_tracker import GoalTracker

console = Console()


@click.group()
def cli():
    """Polymarket Trading Bot - Adaptive $10 -> $1000"""
    pass


@cli.command()
@click.option("--paper/--live", default=True, help="Paper or live trading mode")
def start(paper):
    """Start the trading bot."""
    settings = get_settings()
    settings.paper_trading = paper
    mode = "PAPER" if paper else "LIVE"

    if not paper and not settings.has_credentials():
        console.print("[red]No credentials configured. Run: python -m src.main setup[/red]")
        return

    console.print(Panel(
        f"[bold green]Starting bot in {mode} mode[/bold green]\n"
        f"Capital: ${settings.starting_capital} -> ${settings.target_balance}\n"
        f"Target: {settings.target_days} days\n"
        f"Press Ctrl+C to stop",
        title="Poly Trade Bot",
    ))

    from src.bot import Bot
    bot = Bot(settings)
    bot.build()
    bot.run()


@cli.command()
def status():
    """Show current balance and positions."""
    settings = get_settings()
    conn = init_db(settings.db_path)
    trade_log = TradeLog(conn)

    positions = trade_log.get_open_positions()
    recent = trade_log.get_recent_trades(10)

    table = Table(title="Open Positions")
    table.add_column("ID")
    table.add_column("Market")
    table.add_column("Outcome")
    table.add_column("Entry")
    table.add_column("Size")
    table.add_column("Cost")

    for p in positions:
        table.add_row(
            str(p["id"]),
            (p.get("market_question") or "")[:40],
            p["outcome"],
            f"${p['entry_price']:.3f}",
            f"{p['size']:.2f}",
            f"${p['cost']:.2f}",
        )
    console.print(table)

    if recent:
        trade_table = Table(title="Recent Trades")
        trade_table.add_column("Time")
        trade_table.add_column("Strategy")
        trade_table.add_column("Outcome")
        trade_table.add_column("Price")
        trade_table.add_column("Size")
        trade_table.add_column("Status")
        trade_table.add_column("PnL")

        for t in recent:
            pnl = t.get("pnl")
            pnl_str = f"${pnl:.2f}" if pnl is not None else "-"
            trade_table.add_row(
                (t.get("timestamp") or "")[:19],
                t["strategy"],
                t["outcome"],
                f"${t['price']:.3f}",
                f"{t['size']:.2f}",
                t["status"],
                pnl_str,
            )
        console.print(trade_table)


@cli.command()
def goal():
    """Show goal progress tracker."""
    settings = get_settings()
    conn = init_db(settings.db_path)
    trade_log = TradeLog(conn)

    snapshots = trade_log.get_daily_snapshots(30)
    tracker = GoalTracker(settings.starting_capital, settings.target_balance, settings.target_days)

    current_balance = settings.starting_capital
    for snap in reversed(snapshots):
        tracker.record_balance(snap["balance"])
        current_balance = snap["balance"]

    status = tracker.get_status(current_balance)

    console.print(Panel(
        f"[bold]Goal: ${status.starting_capital:.2f} -> ${status.target_balance:.2f}[/bold]\n\n"
        f"Current Balance: [green]${status.current_balance:.2f}[/green]\n"
        f"Progress: {status.progress_pct:.1f}%\n"
        f"Days Elapsed: {status.days_elapsed:.1f} / {status.target_days}\n"
        f"Days Remaining: {status.days_remaining:.1f}\n\n"
        f"Required Daily Rate: {status.required_daily_rate:.2%}\n"
        f"Actual 7-Day Rate: {status.actual_7day_rate:.2%}\n"
        f"On Track: {'Yes' if status.on_track else 'No'}\n"
        f"Projected Days to Target: {status.projected_days:.0f}",
        title="Goal Progress",
    ))


@cli.command()
def history():
    """Show trade history."""
    settings = get_settings()
    conn = init_db(settings.db_path)
    trade_log = TradeLog(conn)
    trades = trade_log.get_recent_trades(50)

    table = Table(title=f"Trade History ({len(trades)} most recent)")
    table.add_column("Time")
    table.add_column("Strategy")
    table.add_column("Market")
    table.add_column("Outcome")
    table.add_column("Price")
    table.add_column("Size")
    table.add_column("Cost")
    table.add_column("PnL")
    table.add_column("Mode")

    for t in trades:
        pnl = t.get("pnl")
        pnl_str = f"${pnl:.2f}" if pnl is not None else "-"
        mode = "paper" if t.get("paper_trade") else "live"
        table.add_row(
            (t.get("timestamp") or "")[:19],
            t["strategy"],
            (t.get("market_question") or "")[:30],
            t["outcome"],
            f"${t['price']:.3f}",
            f"{t['size']:.2f}",
            f"${t['cost']:.2f}",
            pnl_str,
            mode,
        )
    console.print(table)


@cli.command()
def setup():
    """Run credential setup wizard."""
    console.print("[bold]Polymarket Credential Setup[/bold]\n")
    console.print("This will run the interactive setup wizard.")
    console.print("Run: python scripts/setup_credentials.py\n")

    from scripts.setup_credentials import run_setup
    run_setup()


if __name__ == "__main__":
    cli()
