"""Point-in-time replay of the strategy over history.

Run every proposed change through this before trusting it. At each decision date the strategy
sees only what had been accepted into the journal by then (``decide(at=...)`` reads snapshots,
revisions and receipts as of that time) and only the prices up to that date. The book is marked
from the previous decision's prices, traded inside the turnover budget, and charged costs on the
turnover actually traded.

Nothing here contacts a broker. Prices are inputs you supply; macro data comes from the journal.
"""
import math
import datetime as dt

from asset_classes import blended_vol

CASH_KEYS = {'TBILL', 'CASH'}


def _day(x):
    if isinstance(x, dt.datetime):
        return x.date().isoformat()
    if isinstance(x, dt.date):
        return x.isoformat()
    return str(x)[:10]


def _prices_until(prices, day):
    """``{asset: [price, ...]}`` using only observations dated on or before ``day``."""
    out = {}
    for asset, rows in (prices or {}).items():
        vals = [float(p) for d, p in rows if _day(d) <= day and p is not None and float(p) > 0]
        if vals:
            out[asset] = vals
    return out


def stats(navs, periods_per_year, turnovers=None, costs=None):
    """Annualised return, volatility, Sharpe (rf = 0), max drawdown and trading totals."""
    rets = [b / a - 1.0 for a, b in zip(navs, navs[1:]) if a > 0]
    n = len(rets)
    if n == 0:
        return {'periods': 0}
    total = navs[-1] / navs[0] - 1.0
    years = n / float(periods_per_year)
    ann = (navs[-1] / navs[0]) ** (1.0 / years) - 1.0 if years > 0 and navs[-1] > 0 else None
    mean = sum(rets) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / max(1, n - 1))
    peak, mdd = navs[0], 0.0
    for v in navs:
        peak = max(peak, v)
        mdd = max(mdd, 1.0 - v / peak)
    return {
        'periods': n,
        'total_return': round(total, 6),
        'annual_return': round(ann, 6) if ann is not None else None,
        'annual_vol': round(sd * math.sqrt(periods_per_year), 6),
        'sharpe': round(mean / sd * math.sqrt(periods_per_year), 4) if sd > 1e-12 else None,
        'max_drawdown': round(mdd, 6),
        'avg_turnover': round(sum(turnovers) / len(turnovers), 6) if turnovers else None,
        'total_costs': round(sum(costs), 6) if costs else None,
    }


def replay(c, dates, prices, asset_vols=None, cost_bps=10.0, target_vol=0.10, max_turnover=0.50,
           quadrant_tilt=0.5, start_nav=1.0, periods_per_year=None, decide_fn=None,
           use_trend=True):
    """Replay the strategy at each of ``dates`` (oldest first) and return the equity curve.

    ``prices`` is ``{asset: [(date, price), ...]}`` oldest first. Volatilities come from the
    prices known at each date (``asset_classes.blended_vol``) unless ``asset_vols`` is given.
    ``decide_fn`` defaults to ``strategy_engine.decide`` and is injectable for testing.

    Returns ``{'strategy': stats, 'benchmark': stats, 'curve': [...], 'look_ahead': ...}``. The
    benchmark is an equal-weight, buy-and-hold book of the same assets, so you can see whether
    the macro layer adds anything over doing nothing clever.
    """
    if decide_fn is None:
        from strategy_engine import decide as decide_fn
    dates = list(dates or [])
    if len(dates) < 2:
        return {'error': 'need at least two decision dates'}
    if periods_per_year is None:
        span = (dt.date.fromisoformat(_day(dates[-1])) - dt.date.fromisoformat(_day(dates[0]))).days
        periods_per_year = (len(dates) - 1) / (span / 365.25) if span > 0 else 252

    nav = float(start_nav)
    weights, marks = {}, {}
    navs, curve, turnovers, costs = [nav], [], [], []
    bench_nav, bench_marks, bench_navs = float(start_nav), {}, [float(start_nav)]
    assets = sorted(prices or {})

    for i, when in enumerate(dates):
        day = _day(when)
        known = _prices_until(prices, day)
        latest = {a: px[-1] for a, px in known.items()}

        # 1. Mark the held book from the previous decision's prices to today's.
        if i > 0:
            gross = 0.0
            for a, w in weights.items():
                if a in CASH_KEYS or a not in latest or a not in marks:
                    gross += w
                    continue
                gross += w * latest[a] / marks[a]
            cash_w = max(0.0, 1.0 - sum(weights.values()))
            mult = gross + cash_w
            nav *= mult
            weights = {a: (w * (latest[a] / marks[a]) if a in latest and a in marks
                           and a not in CASH_KEYS else w) / mult for a, w in weights.items()} if mult > 0 else {}
            # Benchmark: equal weight, bought on day one, held.
            if bench_marks:
                bench_nav = float(start_nav) / len(bench_marks) * sum(
                    latest.get(a, m) / m for a, m in bench_marks.items())
            bench_navs.append(bench_nav)
            navs.append(nav)
        elif assets:
            bench_marks = {a: latest[a] for a in assets if a in latest}

        # 2. Decide with information available on this date only.
        vols = dict(asset_vols) if asset_vols else {
            a: v for a, v in ((a, blended_vol(px)) for a, px in known.items()) if v}
        decision = decide_fn(c, None, vols, prev_weights=dict(weights), at=when,
                             equity_curve=list(navs), max_turnover=max_turnover,
                             target_vol=target_vol, quadrant_tilt=quadrant_tilt,
                             price_series=known if use_trend else None)
        booked = {a: float(w) for a, w in (decision.get('booked_weights') or {}).items()
                  if a not in CASH_KEYS and float(w) > 0}
        traded = sum(abs(booked.get(a, 0.0) - weights.get(a, 0.0)) for a in set(booked) | set(weights))
        cost = nav * traded * float(cost_bps) / 10000.0
        nav -= cost
        navs[-1] = nav
        turnovers.append(traded)
        costs.append(cost / max(1e-12, nav + cost))
        weights = booked
        marks = {a: latest[a] for a in weights if a in latest}
        curve.append({'date': day, 'nav': round(nav, 6), 'regime': decision.get('regime'),
                      'quadrant': decision.get('quadrant'), 'weights': booked,
                      'turnover': round(traded, 6), 'data_outage': decision.get('data_outage')})

    return {
        'strategy': stats(navs, periods_per_year, turnovers, costs),
        'benchmark': stats(bench_navs, periods_per_year),
        'periods_per_year': round(periods_per_year, 3),
        'curve': curve,
        'look_ahead': 'none: each decision reads the journal and prices as of its own date',
    }
