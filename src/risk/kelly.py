def half_kelly(win_prob: float, payout_ratio: float, available_balance: float,
               min_trade: float = 0.50, max_trade_pct: float = 0.10) -> float:
    if win_prob <= 0 or payout_ratio <= 0 or available_balance <= 0:
        return 0.0

    loss_prob = 1.0 - win_prob
    kelly_fraction = (win_prob * payout_ratio - loss_prob) / payout_ratio

    if kelly_fraction <= 0:
        return 0.0

    position_size = kelly_fraction * 0.5 * available_balance
    max_trade = available_balance * max_trade_pct
    position_size = max(min(position_size, max_trade), 0.0)

    if position_size < min_trade:
        position_size = min_trade  # bump to CLOB minimum; caller checks cost vs balance

    return round(position_size, 2)


def calc_payout_ratio(entry_price: float) -> float:
    if entry_price <= 0 or entry_price >= 1:
        return 0.0
    return (1.0 - entry_price) / entry_price
