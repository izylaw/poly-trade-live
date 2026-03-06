#!/usr/bin/env python3
"""Interactive credential setup wizard for Polymarket trading bot."""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm

console = Console()


def run_setup():
    console.print(Panel(
        "[bold]Polymarket Trading Bot - Credential Setup[/bold]\n\n"
        "This wizard will help you configure your Polymarket credentials.\n"
        "You'll need:\n"
        "  1. A Polygon wallet private key\n"
        "  2. USDC.e funded on Polygon\n"
        "  3. API credentials (we'll derive them for you)",
        title="Setup Wizard",
    ))

    env_path = Path(".env")
    existing = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    # Step 1: Private key
    console.print("\n[bold cyan]Step 1: Polygon Wallet[/bold cyan]")
    console.print("You need a Polygon wallet with USDC.e for trading.")
    console.print("If you don't have one, create one at https://metamask.io")
    console.print("[yellow]NEVER share your private key with anyone.[/yellow]\n")

    private_key = Prompt.ask(
        "Enter your Polygon wallet private key (0x...)",
        default=existing.get("POLY_PRIVATE_KEY", ""),
    )

    if not private_key:
        console.print("[red]Private key is required. Exiting.[/red]")
        return

    # Step 2: Fund wallet
    console.print("\n[bold cyan]Step 2: Fund Your Wallet[/bold cyan]")
    console.print("Ensure your wallet has USDC.e on Polygon.")
    console.print("Bridge from Ethereum: https://wallet.polygon.technology/bridge")
    console.print("Or buy directly on Polygon via an exchange.\n")

    if not Confirm.ask("Is your wallet funded with USDC.e on Polygon?"):
        console.print("[yellow]Please fund your wallet before continuing.[/yellow]")
        console.print("You can still save credentials and fund later.\n")

    # Step 3: Derive API credentials
    console.print("\n[bold cyan]Step 3: API Credentials[/bold cyan]")

    has_creds = existing.get("POLY_API_KEY")
    if has_creds:
        console.print("Existing API credentials found in .env")
        if not Confirm.ask("Re-derive API credentials?"):
            api_key = existing.get("POLY_API_KEY", "")
            api_secret = existing.get("POLY_API_SECRET", "")
            api_passphrase = existing.get("POLY_API_PASSPHRASE", "")
        else:
            has_creds = False

    if not has_creds:
        if Confirm.ask("Derive API credentials from your private key?"):
            try:
                from py_clob_client.client import ClobClient
                client = ClobClient(
                    "https://clob.polymarket.com",
                    key=private_key,
                    chain_id=137,
                )
                console.print("Deriving API credentials...")
                creds = client.derive_api_key()
                api_key = creds.get("apiKey", "")
                api_secret = creds.get("secret", "")
                api_passphrase = creds.get("passphrase", "")
                console.print("[green]API credentials derived successfully![/green]")
            except Exception as e:
                console.print(f"[red]Failed to derive credentials: {e}[/red]")
                console.print("You can enter them manually instead.\n")
                api_key = Prompt.ask("API Key", default="")
                api_secret = Prompt.ask("API Secret", default="")
                api_passphrase = Prompt.ask("API Passphrase", default="")
        else:
            api_key = Prompt.ask("API Key", default=existing.get("POLY_API_KEY", ""))
            api_secret = Prompt.ask("API Secret", default=existing.get("POLY_API_SECRET", ""))
            api_passphrase = Prompt.ask("API Passphrase", default=existing.get("POLY_API_PASSPHRASE", ""))

    # Step 4: Trading config
    console.print("\n[bold cyan]Step 4: Trading Configuration[/bold cyan]")
    starting_capital = Prompt.ask("Starting capital (USDC)", default=existing.get("STARTING_CAPITAL", "10"))
    target_balance = Prompt.ask("Target balance (USDC)", default=existing.get("TARGET_BALANCE", "1000"))
    target_days = Prompt.ask("Target days", default=existing.get("TARGET_DAYS", "60"))
    paper_trading = Confirm.ask("Start in paper trading mode?", default=True)

    # Save .env
    env_content = f"""POLY_PRIVATE_KEY={private_key}
POLY_API_KEY={api_key}
POLY_API_SECRET={api_secret}
POLY_API_PASSPHRASE={api_passphrase}
POLYGON_RPC_URL={existing.get("POLYGON_RPC_URL", "https://polygon-rpc.com")}
STARTING_CAPITAL={starting_capital}
TARGET_BALANCE={target_balance}
TARGET_DAYS={target_days}
PAPER_TRADING={"true" if paper_trading else "false"}
LOG_LEVEL=INFO
"""
    env_path.write_text(env_content)
    console.print(f"\n[green]Credentials saved to {env_path}[/green]")

    # Step 5: Verify
    if api_key and Confirm.ask("\nVerify connectivity with a balance check?"):
        try:
            from src.config.settings import get_settings
            from src.market_data.clob_client import PolymarketClobClient
            settings = get_settings()
            clob = PolymarketClobClient(settings)
            balance = clob.get_balance()
            console.print(f"[green]Connected! Balance: ${balance:.2f} USDC[/green]")
        except Exception as e:
            console.print(f"[yellow]Connection test failed: {e}[/yellow]")
            console.print("This may be normal if wallet isn't funded yet.")

    console.print(Panel(
        "[bold green]Setup complete![/bold green]\n\n"
        f"Start the bot with:\n"
        f"  python -m src.main start {'--paper' if paper_trading else '--live'}",
        title="Done",
    ))


if __name__ == "__main__":
    run_setup()
