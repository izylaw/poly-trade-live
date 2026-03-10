#!/usr/bin/env python3
"""
Polymarket Copy Trade Bot (dry-run first).

- Monitors up to N wallets (default 100)
- Pulls recent TRADE activity from Polymarket Data API
- Mirrors trades (simulated in dry-run mode)
- Applies risk controls before mirroring
- Writes structured logs to logs/copy_trade_*.jsonl
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

DATA_API = "https://data-api.polymarket.com"


@dataclass
class CopyTradeConfig:
    wallets_file: Path
    interval_sec: int = 20
    lookback_sec: int = 120
    max_wallets: int = 100
    dry_run: bool = True
    max_mirror_usd_per_trade: float = 15.0
    daily_loss_limit_usd: float = 50.0
    max_trades_per_wallet_per_hour: int = 30
    max_total_mirrors_per_cycle: int = 200
    out_file: Path = Path("logs/copy_trade_events.jsonl")


class RiskController:
    def __init__(self, cfg: CopyTradeConfig):
        self.cfg = cfg
        self._wallet_trade_timestamps: dict[str, list[float]] = {}
        self._daily_realized_pnl_usd = 0.0
        self._day_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self):
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._day_key:
            self._day_key = day
            self._daily_realized_pnl_usd = 0.0
            self._wallet_trade_timestamps.clear()

    def allow(self, wallet: str, intended_usd: float, now_ts: float) -> tuple[bool, str]:
        self._roll_day()

        if self._daily_realized_pnl_usd <= -abs(self.cfg.daily_loss_limit_usd):
            return False, "daily_loss_limit_reached"

        if intended_usd <= 0:
            return False, "invalid_trade_size"

        if intended_usd > self.cfg.max_mirror_usd_per_trade:
            return False, "max_mirror_usd_per_trade"

        trades = self._wallet_trade_timestamps.setdefault(wallet, [])
        one_hour_ago = now_ts - 3600
        trades[:] = [t for t in trades if t >= one_hour_ago]
        if len(trades) >= self.cfg.max_trades_per_wallet_per_hour:
            return False, "wallet_rate_limit"

        trades.append(now_ts)
        return True, "ok"


class CopyTradeBot:
    def __init__(self, cfg: CopyTradeConfig):
        self.cfg = cfg
        self.risk = RiskController(cfg)
        self._seen_trade_keys: set[str] = set()
        self.cfg.out_file.parent.mkdir(parents=True, exist_ok=True)

    def load_wallets(self) -> list[str]:
        content = self.cfg.wallets_file.read_text(encoding="utf-8").strip()
        wallets: list[str] = []

        if content.startswith("["):
            arr = json.loads(content)
            wallets = [str(x).strip().lower() for x in arr]
        else:
            for line in content.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                wallets.append(line.lower())

        wallets = [w for w in wallets if w.startswith("0x") and len(w) == 42]
        dedup = []
        seen = set()
        for w in wallets:
            if w not in seen:
                seen.add(w)
                dedup.append(w)
        return dedup[: self.cfg.max_wallets]

    def fetch_wallet_activity(self, wallet: str, start_ts: int) -> list[dict[str, Any]]:
        params = {
            "user": wallet,
            "type": "TRADE",
            "start": start_ts,
            "limit": 500,
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        }
        r = requests.get(f"{DATA_API}/activity", params=params, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json()
        return data if isinstance(data, list) else []

    def _event_key(self, e: dict[str, Any]) -> str:
        return f"{e.get('transactionHash','')}-{e.get('asset','')}-{e.get('timestamp','')}-{e.get('side','')}"

    def _log(self, payload: dict[str, Any]):
        payload = {"ts": datetime.now(timezone.utc).isoformat(), **payload}
        with self.cfg.out_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

    def _build_mirror_order(self, source_wallet: str, ev: dict[str, Any]) -> dict[str, Any]:
        usdc_size = float(ev.get("usdcSize") or 0.0)
        side = str(ev.get("side") or "").upper()
        asset = str(ev.get("asset") or "")
        price = float(ev.get("price") or 0.0)
        size = float(ev.get("size") or 0.0)

        mirror_usd = min(usdc_size if usdc_size > 0 else size * price, self.cfg.max_mirror_usd_per_trade)

        return {
            "source_wallet": source_wallet,
            "source_tx": ev.get("transactionHash"),
            "timestamp": ev.get("timestamp"),
            "condition_id": ev.get("conditionId"),
            "asset": asset,
            "side": side,
            "price": price,
            "size": size,
            "source_usdc_size": usdc_size,
            "mirror_notional_usd": round(mirror_usd, 4),
            "title": ev.get("title"),
            "outcome": ev.get("outcome"),
            "slug": ev.get("slug"),
        }

    def run_cycle(self, wallets: list[str]) -> dict[str, int]:
        start_ts = int(time.time()) - self.cfg.lookback_sec
        collected: list[tuple[str, dict[str, Any]]] = []

        with ThreadPoolExecutor(max_workers=min(24, max(4, len(wallets)))) as pool:
            futures = {pool.submit(self.fetch_wallet_activity, w, start_ts): w for w in wallets}
            for fut in as_completed(futures):
                wallet = futures[fut]
                try:
                    rows = fut.result()
                    for e in rows:
                        collected.append((wallet, e))
                except Exception as e:
                    self._log({"event": "wallet_fetch_error", "wallet": wallet, "error": str(e)})

        mirrored = 0
        skipped = 0
        processed = 0

        collected.sort(key=lambda x: int(x[1].get("timestamp") or 0))

        for wallet, ev in collected:
            if mirrored >= self.cfg.max_total_mirrors_per_cycle:
                break

            if str(ev.get("type")) != "TRADE":
                continue

            key = self._event_key(ev)
            if key in self._seen_trade_keys:
                continue
            self._seen_trade_keys.add(key)
            processed += 1

            order = self._build_mirror_order(wallet, ev)
            allow, reason = self.risk.allow(wallet, order["mirror_notional_usd"], time.time())
            if not allow:
                skipped += 1
                self._log({"event": "trade_skipped", "reason": reason, "order": order})
                continue

            if self.cfg.dry_run:
                mirrored += 1
                self._log({"event": "dry_run_mirror", "order": order})
            else:
                # Live execution hook intentionally explicit; keep dry-run safe by default.
                mirrored += 1
                self._log({"event": "live_mirror_not_implemented", "order": order})

        return {"processed": processed, "mirrored": mirrored, "skipped": skipped}

    def run(self):
        wallets = self.load_wallets()
        if not wallets:
            raise SystemExit("No valid wallets loaded.")

        print(f"CopyTradeBot started | wallets={len(wallets)} | dry_run={self.cfg.dry_run} | interval={self.cfg.interval_sec}s")
        self._log({"event": "bot_start", "wallet_count": len(wallets), "dry_run": self.cfg.dry_run})

        while True:
            try:
                s = self.run_cycle(wallets)
                print(f"cycle processed={s['processed']} mirrored={s['mirrored']} skipped={s['skipped']}")
                self._log({"event": "cycle_summary", **s})
                time.sleep(self.cfg.interval_sec)
            except KeyboardInterrupt:
                self._log({"event": "bot_stop", "reason": "keyboard_interrupt"})
                print("CopyTradeBot stopped")
                break
            except Exception as e:
                self._log({"event": "cycle_error", "error": str(e)})
                print(f"cycle error: {e}")
                time.sleep(max(3, self.cfg.interval_sec // 2))


def parse_args() -> CopyTradeConfig:
    p = argparse.ArgumentParser(description="Polymarket copy trade bot")
    p.add_argument("--wallets-file", default="scanner/results.json", help="Path to wallet list (.txt/.json).")
    p.add_argument("--interval-sec", type=int, default=20)
    p.add_argument("--lookback-sec", type=int, default=120)
    p.add_argument("--max-wallets", type=int, default=100)
    p.add_argument("--dry-run", action="store_true", default=True)
    p.add_argument("--live", action="store_true", help="Disable dry-run (execution hook placeholder).")
    p.add_argument("--max-mirror-usd-per-trade", type=float, default=15.0)
    p.add_argument("--daily-loss-limit-usd", type=float, default=50.0)
    p.add_argument("--max-trades-per-wallet-per-hour", type=int, default=30)
    p.add_argument("--max-total-mirrors-per-cycle", type=int, default=200)
    p.add_argument("--out-file", default="logs/copy_trade_events.jsonl")

    a = p.parse_args()

    dry_run = False if a.live else bool(a.dry_run)

    return CopyTradeConfig(
        wallets_file=Path(a.wallets_file),
        interval_sec=a.interval_sec,
        lookback_sec=a.lookback_sec,
        max_wallets=a.max_wallets,
        dry_run=dry_run,
        max_mirror_usd_per_trade=a.max_mirror_usd_per_trade,
        daily_loss_limit_usd=a.daily_loss_limit_usd,
        max_trades_per_wallet_per_hour=a.max_trades_per_wallet_per_hour,
        max_total_mirrors_per_cycle=a.max_total_mirrors_per_cycle,
        out_file=Path(a.out_file),
    )


def _wallets_from_scanner_results(results_path: Path, out_wallets_path: Path, top_n: int = 100):
    if not results_path.exists():
        return
    try:
        data = json.loads(results_path.read_text(encoding="utf-8"))
        rows = data.get("top_100") or []
        wallets = [r.get("wallet") for r in rows if isinstance(r, dict) and r.get("wallet")]
        wallets = [w for w in wallets if isinstance(w, str) and w.startswith("0x")][:top_n]
        out_wallets_path.parent.mkdir(parents=True, exist_ok=True)
        out_wallets_path.write_text("\n".join(wallets) + "\n", encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    cfg = parse_args()

    # Convenience: if wallets-file points to scanner/results.json, auto-extract top_100 wallet list.
    if cfg.wallets_file.name == "results.json":
        generated = cfg.wallets_file.parent / "top_wallets.txt"
        _wallets_from_scanner_results(cfg.wallets_file, generated, top_n=cfg.max_wallets)
        if generated.exists():
            cfg.wallets_file = generated

    bot = CopyTradeBot(cfg)
    bot.run()
