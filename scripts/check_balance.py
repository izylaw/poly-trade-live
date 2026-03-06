#!/usr/bin/env python3
"""Quick balance checker for Polymarket account."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from src.config.settings import get_settings
from src.market_data.clob_client import PolymarketClobClient

console = Console()


def main():
    settings = get_settings()
    if not settings.has_credentials():
        console.print("[red]No credentials configured. Run: python scripts/setup_credentials.py[/red]")
        return

    clob = PolymarketClobClient(settings)
    try:
        balance = clob.get_balance()
        console.print(f"[green]Balance: ${balance:.2f} USDC[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


if __name__ == "__main__":
    main()
