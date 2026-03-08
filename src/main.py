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
    table.add_column("Status")
    table.add_column("PnL")
    table.add_column("Mode")

    for t in trades:
        pnl = t.get("pnl")
        if pnl is not None:
            pnl_str = f"[green]+${pnl:.2f}[/green]" if pnl > 0 else f"[red]-${abs(pnl):.2f}[/red]"
        else:
            pnl_str = "-"

        status = t.get("status", "?")
        status_style = {
            "filled": "[green]filled[/green]",
            "pending": "[yellow]pending[/yellow]",
            "cancelled": "[dim]cancelled[/dim]",
        }.get(status, status)

        mode = "paper" if t.get("paper_trade") else "live"
        table.add_row(
            (t.get("timestamp") or "")[:19],
            t["strategy"],
            (t.get("market_question") or "")[:30],
            t["outcome"],
            f"${t['price']:.3f}",
            f"{t['size']:.2f}",
            f"${t['cost']:.2f}",
            status_style,
            pnl_str,
            mode,
        )
    console.print(table)


@cli.command()
@click.option("--top", default=50, help="Number of candidates to check against CLOB")
def xray(top):
    """Market X-ray: diagnose why the bot isn't finding trades."""
    from src.market_data.gamma_client import GammaClient
    from src.market_data.clob_client import PolymarketClobClient
    from src.market_data.market_filter import MarketFilter
    from src.market_data.market_scanner import MarketScanner
    from src.strategies.high_probability import _parse_outcome_prices
    import json

    settings = get_settings()
    setup_logger("poly-trade", settings.log_level, settings.log_dir)

    console.print(Panel("[bold]Market X-ray[/bold]\nScanning markets and comparing Gamma vs CLOB prices...", title="Diagnostics"))

    # 1. Fetch & filter markets (same as the bot)
    gamma = GammaClient()
    clob = PolymarketClobClient(settings)
    market_filter = MarketFilter(settings)
    scanner = MarketScanner(settings, gamma, market_filter, clob_client=clob)
    markets = scanner.scan()

    console.print(f"\n[bold]Filtered markets:[/bold] {len(markets)}")

    # 2. Pre-filter same as high_probability strategy
    min_p = settings.high_prob_min_price
    max_p = settings.high_prob_max_price
    margin = 0.03
    candidates = []
    for m in markets:
        tokens = m.get("clobTokenIds") or []
        if len(tokens) < 2:
            continue
        outcome_prices = _parse_outcome_prices(m)
        for i, price in enumerate(outcome_prices):
            if i < len(tokens) and (min_p - margin) <= price <= (max_p + margin):
                candidates.append((m, i, price, tokens[i]))
                break

    console.print(f"[bold]Gamma candidates in {min_p:.2f}-{max_p:.2f} range:[/bold] {len(candidates)}")

    if not candidates:
        console.print("[red]No candidates found. The scanner filters are too strict or no markets exist in this range.[/red]")
        return

    # 3. Sort by Gamma price and check top N against CLOB
    candidates.sort(key=lambda x: x[2], reverse=True)
    check = candidates[:top]
    token_ids = [c[3] for c in check]

    console.print(f"\nFetching CLOB orderbooks for top {len(check)} candidates...")
    price_map = clob.get_orderbooks_batch(token_ids)

    # 4. Analyze
    results = []
    for market, idx, gamma_price, token_id in check:
        clob_data = price_map.get(token_id)
        bid = clob_data["bid"] if clob_data else None
        ask = clob_data["ask"] if clob_data else None
        spread = (ask - bid) if (bid is not None and ask is not None and ask < 1.0) else None
        question = (market.get("question") or "Unknown")[:60]
        results.append({
            "question": question,
            "gamma": gamma_price,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "token_id": token_id[:12],
            "volume": float(market.get("volume", 0) or 0),
            "in_range": ask is not None and min_p <= ask <= max_p,
            "maker_viable": bid is not None and min_p <= (bid + 0.01) <= max_p,
        })

    # 5. Report: Summary stats
    has_clob = [r for r in results if r["ask"] is not None]
    in_range = [r for r in results if r["in_range"]]
    maker_viable = [r for r in results if r["maker_viable"]]
    no_asks = [r for r in has_clob if r["ask"] >= 1.0]

    console.print("\n")
    summary = Table(title="Summary", show_header=False)
    summary.add_column("Metric", style="bold")
    summary.add_column("Value")
    summary.add_row("Candidates checked", str(len(check)))
    summary.add_row("CLOB data returned", str(len(has_clob)))
    summary.add_row("No asks (ask=1.0)", str(len(no_asks)))
    summary.add_row(f"Ask in range ({min_p}-{max_p})", f"[green]{len(in_range)}[/green]" if in_range else f"[red]{len(in_range)}[/red]")
    summary.add_row(f"Maker bid viable ({min_p}-{max_p})", f"[green]{len(maker_viable)}[/green]" if maker_viable else f"[yellow]{len(maker_viable)}[/yellow]")
    console.print(summary)

    # 6. Report: Ask price distribution
    asks = [r["ask"] for r in has_clob if r["ask"] is not None]
    if asks:
        buckets = {
            "< 0.80": 0, "0.80-0.85": 0, "0.85-0.88": 0,
            "0.88-0.90": 0, "0.90-0.92": 0, "0.92-0.95": 0,
            "0.95-0.98": 0, "0.98-0.99": 0, "0.99-1.00": 0, "= 1.00 (no asks)": 0,
        }
        for a in asks:
            if a >= 1.0: buckets["= 1.00 (no asks)"] += 1
            elif a >= 0.99: buckets["0.99-1.00"] += 1
            elif a >= 0.98: buckets["0.98-0.99"] += 1
            elif a >= 0.95: buckets["0.95-0.98"] += 1
            elif a >= 0.92: buckets["0.92-0.95"] += 1
            elif a >= 0.90: buckets["0.90-0.92"] += 1
            elif a >= 0.88: buckets["0.88-0.90"] += 1
            elif a >= 0.85: buckets["0.85-0.88"] += 1
            elif a >= 0.80: buckets["0.80-0.85"] += 1
            else: buckets["< 0.80"] += 1

        dist_table = Table(title="CLOB Ask Price Distribution")
        dist_table.add_column("Price Range")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")
        for label, count in buckets.items():
            bar = "#" * count
            dist_table.add_row(label, str(count), bar)
        console.print(dist_table)

    # 7. Report: Gamma vs CLOB comparison (top mismatches)
    detail_table = Table(title=f"Top {min(15, len(results))} Candidates: Gamma vs CLOB")
    detail_table.add_column("Market", max_width=45)
    detail_table.add_column("Gamma", justify="right")
    detail_table.add_column("CLOB Bid", justify="right")
    detail_table.add_column("CLOB Ask", justify="right")
    detail_table.add_column("Spread", justify="right")
    detail_table.add_column("Status")

    for r in results[:15]:
        gamma_str = f"{r['gamma']:.3f}"
        bid_str = f"{r['bid']:.3f}" if r['bid'] is not None else "N/A"
        ask_str = f"{r['ask']:.3f}" if r['ask'] is not None else "N/A"
        spread_str = f"{r['spread']:.3f}" if r['spread'] is not None else "N/A"

        if r["in_range"]:
            status = "[green]TRADEABLE[/green]"
        elif r["maker_viable"]:
            status = "[yellow]MAKER OK[/yellow]"
        elif r["ask"] is not None and r["ask"] >= 1.0:
            status = "[red]NO ASKS[/red]"
        elif r["ask"] is not None and r["ask"] > max_p:
            status = f"[red]ASK>{max_p}[/red]"
        elif r["ask"] is not None and r["ask"] < min_p:
            status = f"[dim]ASK<{min_p}[/dim]"
        else:
            status = "[dim]NO DATA[/dim]"

        detail_table.add_row(r["question"], gamma_str, bid_str, ask_str, spread_str, status)

    console.print(detail_table)

    # 8. Report: Spread analysis for viable markets
    spreads = [r["spread"] for r in has_clob if r["spread"] is not None]
    if spreads:
        avg_spread = sum(spreads) / len(spreads)
        tight = [s for s in spreads if s <= 0.03]
        wide = [s for s in spreads if s > 0.10]
        console.print(f"\n[bold]Spread analysis:[/bold] avg={avg_spread:.3f}, tight(<3%)={len(tight)}, wide(>10%)={len(wide)}")

    # 9. Recommendations
    console.print("\n")
    recs = []
    if len(in_range) == 0 and len(no_asks) > len(has_clob) * 0.3:
        recs.append("Many markets have NO asks (empty sell side). Maker bids (option C) would help — place bids below the ask instead of taking.")
    if len(in_range) == 0:
        # Find where the asks actually cluster
        below = [r for r in has_clob if r["ask"] is not None and r["ask"] < min_p]
        above = [r for r in has_clob if r["ask"] is not None and min_p <= r["ask"] <= 1.0 and r["ask"] > max_p]
        if above:
            recs.append(f"{len(above)} markets have asks just above {max_p}. Widening max_price to 0.99 would capture some, but margins are thin (~1% per trade).")
        if below:
            recs.append(f"{len(below)} markets have asks below {min_p}. Consider lowering min_price for higher-margin (but riskier) trades.")
    if len(maker_viable) > 0:
        recs.append(f"{len(maker_viable)} markets are viable for maker bids (bid+$0.01 is in range). The strategy's maker fallback currently only triggers when ask=1.0 — widen it to also trigger when ask > max_price.")
    if not recs:
        recs.append("Markets look healthy! If still no trades, check risk manager limits or circuit breaker state.")

    rec_panel = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(recs))
    console.print(Panel(rec_panel, title="Recommendations", border_style="green"))


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
