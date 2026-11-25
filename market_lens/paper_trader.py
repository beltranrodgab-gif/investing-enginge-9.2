"""Paper-trading account: mark-to-market NAV, budgeted rebalancing and explicit costs.

The account is simulated, never connected to a broker. Two rules shape this module:

* The NAV moves with *prices*, not only with costs. Marking to market is what makes a paper run
  informative; a NAV that can only fall tells you nothing.
* A turnover limit is a **budget**, so a normal rebalance is spread across periods. The one
  deliberate exception is a fail-closed exit: when the strategy has no usable macro input it
  leaves risk entirely, and that exit is labelled ``emergency`` and fully costed rather than
  hidden behind the budget.

Every holding without a usable price is reported in ``unpriced``. It is carried flat and that is
stated, because silently valuing an unpriced asset as unchanged quietly flatters the result.
"""
import math, statistics
from alpha_engine import run_alpha_step, apply_turnover_budget


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean_prices(series):
    out = [_num(p) for p in (series or [])]
    return [p for p in out if p is not None and p > 0]


def realized_vols(price_series, window=20, long_window=250, halflife=30):
    """Annualised volatility from daily prices (see ``asset_classes.blended_vol``).

    The larger of a ``window``-day estimate and a long EWMA estimate: a 20-day figure alone was
    noisy enough to drive needless rebalancing, while a long figure alone reacts too slowly to a
    crash. Non-positive prices are skipped.
    """
    from asset_classes import blended_vol
    vols = {}
    for asset, px in (price_series or {}).items():
        clean = _clean_prices(px)
        if len(clean) < 3:
            continue
        v = blended_vol(clean, short_window=window, long_window=long_window, halflife=halflife)
        if v is not None:
            vols[asset] = v
    return vols


def _book(weights):
    """Normalise a weights map and return it with the residual cash share."""
    book = {k: float(v) for k, v in (weights or {}).items() if _num(v) is not None and float(v) > 0}
    total = sum(book.values())
    return book, max(0.0, 1.0 - total)


def step_paper_book(target_weights, price_series, prev_weights, nav=100.0, cost_bps=10.0,
                    max_turnover=0.50, regime=None, emergency=False, last_marks=None,
                    data_outage=False):
    """Run one paper-trading period and return the new NAV, book and full cost breakdown.

    **Marking.** ``last_marks`` is ``{asset: price}`` at which each holding was last valued (the
    ``marks`` returned by the previous run). The period return is ``latest / last_mark - 1``, so
    every move since the last run counts, however many bars were skipped. Without a stored mark
    the move between the last two prices is used (``mark_basis`` says which). Previously only the
    last two prices were ever used: a 100 -> 80 -> 79 path booked -1.25% instead of -21%.

    ``data_outage`` executes the outage policy: the target becomes half of the *drifted* book and
    is traded inside the turnover budget (``alpha_engine.outage_target``).

    ``emergency`` remains available for a caller that explicitly wants an unbudgeted exit. It is
    never set automatically any more: a data outage is not a reason to sell everything at once.
    """
    nav0 = _num(nav)
    if nav0 is None or nav0 <= 0:
        raise ValueError('nav must be a positive number')
    prev, cash_w = _book(prev_weights)
    target, target_cash = _book(target_weights)

    # Market move on the book actually held, before any trade.
    ppy, unpriced = {}, []
    marks_in = last_marks or {}
    basis = {}
    for asset in sorted(prev):
        px = _clean_prices((price_series or {}).get(asset))
        mark = _num(marks_in.get(asset))
        if px and mark is not None and mark > 0:
            ppy[asset] = px[-1] / mark - 1.0
            basis[asset] = 'since_last_mark'
        elif len(px) >= 2:
            ppy[asset] = px[-1] / px[-2] - 1.0
            basis[asset] = 'last_two_prices'
        else:
            unpriced.append(asset)
    # A holding with no usable price is carried flat and named in ``unpriced``. It must not raise:
    # one missing price would otherwise abort the whole period, and silently treating it as flat
    # without saying so would flatter the result.
    grown = {a: w * (1.0 + ppy.get(a, 0.0)) for a, w in prev.items()}
    # Cash does not move with prices. The multiplier is therefore (book + cash), and dividing
    # the risky sleeve alone would apply the equity return to the cash share as well.
    multiplier = sum(grown.values()) + cash_w
    drifted = {a: g / multiplier for a, g in grown.items()} if multiplier > 0 else {}
    market_return = multiplier - 1.0
    nav_moved = nav0 * (1.0 + market_return)

    # Rebalance from the drifted book, inside the budget unless this is an explicit emergency.
    if data_outage:
        from alpha_engine import outage_target
        target = outage_target(drifted)
    if emergency:
        booked, turnover, partial = dict(target), sum(
            abs(target.get(k, 0.0) - drifted.get(k, 0.0)) for k in set(target) | set(drifted)), False
    else:
        booked, turnover, partial = apply_turnover_budget(target, drifted, max_turnover)
    cost = nav_moved * turnover * (float(cost_bps) / 10000.0)
    held, cash_out = _book(booked)
    return {
        'nav_before_trade': round(nav0, 4),
        'nav_after_market': round(nav_moved, 4),
        'nav_after_cost': round(nav_moved - cost, 4),
        'market_return_applied': round(market_return, 6),
        'turnover': round(turnover, 6),
        'cost': round(cost, 4),
        'weights': {k: round(v, 9) for k, v in held.items()},
        'cash': round(cash_out, 6),
        'partial_rebalance': partial,
        'emergency': bool(emergency),
        'data_outage': bool(data_outage),
        'mark_basis': basis,
        # Latest usable price per asset: store these and pass them back as ``last_marks``.
        'marks': {a: _clean_prices(px)[-1] for a, px in (price_series or {}).items()
                  if _clean_prices(px)},
        'unpriced': unpriced,
        'priced_weights': round(sum(ppy.get(a, 0.0) * w for a, w in prev.items()), 6),
        'regime': regime,
        'target_cash': round(target_cash, 6),
    }


def step_paper_portfolio(csv_path, price_series, prev_weights, nav=100.0, cost_bps=10.0, at=None,
                         target_vol=0.10, max_turnover=0.50, last_marks=None):
    """CSV-driven variant: size the book from a screened export, then execute it on paper.

    With no usable signal ``run_alpha_step`` reports a data outage; the book then steps toward half
    of its drifted exposure inside the turnover budget, labelled ``data_outage``. It is not sold
    in one unbudgeted go.
    """
    vols = realized_vols(price_series)
    res = run_alpha_step(csv_path, vols, at=at, prev_weights=prev_weights,
                         target_vol=target_vol, max_turnover=max_turnover)
    outage = bool(res.get('data_outage'))
    out = step_paper_book(res.get('weights') or {}, price_series, prev_weights, nav=nav,
                          cost_bps=cost_bps, max_turnover=max_turnover,
                          regime=res.get('regime'), last_marks=last_marks, data_outage=outage)
    if outage:
        out['reason'] = res.get('reason') or 'no usable signal'
    return out


def mark_to_market(weights, price_series, nav, last_marks=None):
    """Value a book at today's prices, returning NAV and each asset's contribution.

    With ``last_marks`` the move is measured from the stored mark, so skipped bars are not lost.
    """
    n = _num(nav)
    if n is None:
        raise ValueError('nav must be numeric')
    contrib, unpriced = {}, []
    for asset, w in (weights or {}).items():
        px = _clean_prices((price_series or {}).get(asset))
        mark = _num((last_marks or {}).get(asset))
        if px and mark is not None and mark > 0:
            contrib[asset] = round(w * (px[-1] / mark - 1.0) * n, 4)
            continue
        if len(px) < 2:
            contrib[asset] = 0.0
            unpriced.append(asset)
            continue
        contrib[asset] = round(w * (px[-1] / px[-2] - 1.0) * n, 4)
    return {'nav': round(n + sum(contrib.values()), 4), 'contributions': contrib,
            'unpriced': unpriced}


def monte_carlo_confidence(active_returns, trials=1000, seed=42):
    """Bootstrap test of active returns: mean, 95% interval and the chance of beating the benchmark."""
    import random
    clean = [_num(r) for r in (active_returns or [])]
    clean = [r for r in clean if r is not None]
    if len(clean) < 6:
        return {'ok': False, 'reason': 'insufficient history'}
    rng = random.Random(seed)
    n = len(clean)
    trials = max(1, int(trials))
    means = sorted(statistics.mean(rng.choices(clean, k=n)) for _ in range(trials))
    lo = means[min(trials - 1, int(0.025 * trials))]
    hi = means[min(trials - 1, int(0.975 * trials))]
    prob_pos = sum(1 for m in means if m > 0) / trials
    return {'ok': True, 'mean': statistics.mean(clean), 'ci_95': [lo, hi], 'prob_beat_benchmark': prob_pos}


def adapt_risk_from_paper(active_returns, min_scale=0.5, max_scale=1.0):
    """Learn a risk scale from paper results using Sharpe-weighted consistency."""
    clean = [_num(r) for r in (active_returns or [])]
    clean = [r for r in clean if r is not None]
    if len(clean) < 5:
        return 0.75
    recent = clean[-10:]
    mean_ret = statistics.mean(recent)
    sd_ret = statistics.stdev(recent) if len(recent) >= 2 else 0.0
    sharpe = (mean_ret / sd_ret) if sd_ret > 1e-9 else (1.0 if mean_ret > 0 else -1.0)
    win_rate = sum(1 for r in recent if r > 0) / len(recent)
    if mean_ret < 0 or win_rate < 0.45 or sharpe < 0:
        return min_scale
    if mean_ret > 0 and win_rate >= 0.55 and sharpe >= 0.30:
        return max_scale
    return 0.75
