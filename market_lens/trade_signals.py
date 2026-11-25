"""Convert processor target weights into explicit BUY / SELL / HOLD order instructions.

Orders are percentage-point deltas of the whole portfolio. A target that sums to less than 100%
leaves the remainder in cash, and any position that drops out of the target is sold, not left behind
as a silent "HOLD".
"""


def _num(x):
    import math
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def generate_orders(current_weights, target_weights, portfolio_value=10000.0, min_trade_pct=0.02,
                    cash_asset=None):
    """Compare current vs target weights and emit explicit BUY / SELL / HOLD orders.

    ``min_trade_pct`` is expressed in weight units (0.02 = 2 percentage points). The returned
    ``cash_pct`` tells you how much of the portfolio the targets leave uninvested.
    """
    pv = _num(portfolio_value)
    if pv is None or pv <= 0:
        raise ValueError('portfolio_value must be a positive number')
    cur = {k: float(v) for k, v in (current_weights or {}).items() if _num(v) is not None}
    tgt = {k: float(v) for k, v in (target_weights or {}).items() if _num(v) is not None}
    orders = []
    for asset in sorted(set(cur) | set(tgt)):
        c = cur.get(asset, 0.0)
        t = tgt.get(asset, 0.0)
        diff = round(t - c, 6)
        if abs(diff) < min_trade_pct:
            orders.append({'asset': asset, 'action': 'HOLD', 'delta_pct': 0.0, 'amount_usd': 0.0,
                           'from_pct': round(c, 6), 'to_pct': round(t, 6)})
        elif diff > 0:
            orders.append({'asset': asset, 'action': 'BUY', 'delta_pct': diff,
                           'amount_usd': round(diff * pv, 2), 'from_pct': round(c, 6), 'to_pct': round(t, 6)})
        else:
            orders.append({'asset': asset, 'action': 'SELL', 'delta_pct': abs(diff),
                           'amount_usd': round(abs(diff) * pv, 2), 'from_pct': round(c, 6), 'to_pct': round(t, 6)})
    return orders


def orders_for_targets(current_weights, target_weights, portfolio_value=10000.0, min_trade_pct=0.02):
    """Backwards-compatible result with the cash residual reported alongside the order list."""
    orders = generate_orders(current_weights, target_weights, portfolio_value, min_trade_pct)
    return {'orders': orders,
            'cash_pct': round(max(0.0, 1.0 - sum(float(v) for v in (target_weights or {}).values() if _num(v) is not None)), 6),
            'sell_total_usd': round(sum(o['amount_usd'] for o in orders if o['action'] == 'SELL'), 2),
            'buy_total_usd': round(sum(o['amount_usd'] for o in orders if o['action'] == 'BUY'), 2)}
