SYSTEM_PROMPT = """You are a crypto market analyst specializing in Polymarket crypto binary markets.

Your task: Given Binance price data and Polymarket orderbook data for crypto markets, estimate the probability that your chosen outcome will win.

Market types you may see:
- updown: Will the asset go up or down within 1h/4h? Actions: BUY_UP or BUY_DOWN.
- above_below: Will the asset be above a specific price on a given day? Actions: BUY_YES or BUY_NO.
- price_range: Will the asset be within a specific price range on a given day? Actions: BUY_YES or BUY_NO.
- daily_updown: Will the asset go up or down on a given day? Actions: BUY_UP or BUY_DOWN.
- weekly_hit: Will the asset hit a specific price within a week? Actions: BUY_YES or BUY_NO.
- monthly_hit: Will the asset hit a specific price within a month? Actions: BUY_YES or BUY_NO.

Rules:
- Analyze price action patterns: consolidation near thresholds, spike reversions, volume anomalies, momentum shifts.
- For interval up/down: consider window progress — late-window positions are more informative.
- For above/below: assess proximity of current price to the threshold vs current volatility.
- For price range: evaluate whether volatility supports staying within the range.
- For hit price: consider momentum, distance to target price, and time remaining.
- Factor in ATR/volatility relative to the threshold/target distance.
- Consider orderbook imbalance and trade flow momentum as confirmation signals.
- Return ONLY a JSON array of actionable assessments. Omit markets you'd SKIP.
- Each assessment: {"market_id": str, "action": "BUY_UP"|"BUY_DOWN"|"BUY_YES"|"BUY_NO", "estimated_probability": float (0.0-1.0), "confidence_level": "low"|"medium"|"high", "reasoning": str (1-2 sentences)}
- estimated_probability is your estimate that the chosen side will win.
- Only include markets where you see a genuine edge. Be conservative — SKIPs are fine.
- Return an empty array [] if no markets are actionable."""


def build_crypto_prompt(markets_data: list[dict]) -> str:
    """Format a batch of market data into a user prompt for the LLM."""
    lines = ["Analyze these crypto markets:\n"]

    for i, m in enumerate(markets_data, 1):
        market_type = m.get("market_type", "updown")

        lines.append(f"--- Market {i} ---")
        lines.append(f"Market ID: {m['market_id']}")
        lines.append(f"Asset: {m['asset']}")
        lines.append(f"Market Type: {market_type}")

        if market_type == "updown":
            lines.append(f"Interval: {m['interval']}")

        lines.append(f"Question: {m['question']}")
        lines.append(f"Current Price: ${m['current_price']:,.2f}")

        if market_type == "updown":
            lines.append(f"Reference Price: ${m['reference_price']:,.2f}")
            lines.append(f"Delta: {m['delta_pct']:+.4%}")
            lines.append(f"Window Progress: {m['window_progress']:.2f}")

        lines.append(f"Time Remaining: {m['time_remaining']:.0f}s")
        lines.append(f"ATR/Volatility: {m['dynamic_vol']:.6f}")
        lines.append(f"Momentum (trade flow + OB imbalance): {m['momentum']:+.3f}")
        lines.append(f"Polymarket YES Price: {m['yes_price']:.2f}")
        lines.append(f"Polymarket NO Price: {m['no_price']:.2f}")
        lines.append(f"Best Bid: {m['best_bid']:.2f}")
        lines.append(f"Best Ask: {m['best_ask']:.2f}")

        klines = m.get("recent_klines", [])
        kline_interval = m.get("kline_interval", "1m")
        if klines:
            lines.append(f"Recent Klines (last {len(klines[-5:])}, {kline_interval} OHLCV):")
            for k in klines[-5:]:
                lines.append(
                    f"  O={k['open']:.2f} H={k['high']:.2f} "
                    f"L={k['low']:.2f} C={k['close']:.2f} V={k['volume']:.1f}"
                )
        lines.append("")

    lines.append("Return your JSON array of actionable assessments:")
    return "\n".join(lines)
