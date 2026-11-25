"""Bridge from the collected journal to a strategy decision.

The strategy modules were previously a library with no caller: ``market_lens.py`` never imported
them, ``growth_z``/``inflation_z`` had no data source, and no command produced a portfolio. This
module assembles the inputs that already exist in SQLite (accepted, point-in-time history), runs the
filters, and returns one explainable decision.

Every read is **point in time**: ``decide(at=T)`` sees only the snapshots, revisions and
receipts that had been accepted by ``T``. That is what makes ``backtest.replay`` honest — a
backtest that reads today's revised data would be using information nobody had at the time.

Everything here is decision support, not an execution feed. It never contacts a broker.
"""
from decimal import Decimal

from sources import M
from store import now, iso, lens, ready_row, feed_state
from signal_filter import (macro_z_scores, classify_macro_quadrant, revision_noise_ok,
                           prune_correlated)
from alpha_engine import (detect_regime, allocate, apply_turnover_budget, drift_check,
                          sentiment_guard, outage_target, trend_tilts, OUTAGE_RISK_SCALE)
from asset_classes import quadrant_tilts, portfolio_vol as _corr_vol
from daily_processor import all_weather_allocation, confirm_regime, drawdown_guard, drawdown_state
from macro_alerts import macro_diffusion, detect_macro_chains
from trade_signals import generate_orders

CURVE_ID = 'UST.DAILY.10Y2Y'
SOFR_ID = 'NYFED.SOFR'
DEFAULT_RF = 0.04


def series_history(c, feed, series_id, limit=300, at=None):
    """Chronological accepted values for one series, as known at ``at`` (default: now)."""
    r = ready_row(c, feed, iso(at or now()))
    if not r:
        return []
    rows = c.execute(
        'select t as period, x as value, flag, pub, pos from snap where feed=? and h=? and parser=? and series=? and x is not null order by t desc limit ?',
        (feed, r['h'], r['parser'], series_id, int(limit))).fetchall()
    out = []
    for row in reversed(rows):
        try:
            out.append({'period': row['period'], 'value': Decimal(row['value'])})
        except Exception:
            continue
    return out


def usable_inputs(c, at=None):
    """Series with enough clean history to be scored at ``at``, plus what had to be rejected."""
    d = at or now()
    usable, rejected = {}, []
    for sid, meta in M.items():
        if meta.get('factor') not in ('ACT-01', 'LAB-02', 'YLD-03', 'INF-01', 'INF-03', 'COM-02'):
            continue
        k = meta['feed']
        try:
            p, last, state = feed_state(c, k, d)
        except Exception:
            rejected.append({'series': sid, 'reason': 'journal unavailable'})
            continue
        if not last or state not in ('accepted', 'overdue'):
            rejected.append({'series': sid, 'reason': f'feed {state}'})
            continue
        if not revision_noise_ok(c, sid, at=d):
            rejected.append({'series': sid, 'reason': 'excessive revisions'})
            continue
        hist = series_history(c, k, sid, at=d)
        if len(hist) < 6:
            rejected.append({'series': sid, 'reason': 'short history'})
            continue
        usable[sid] = [float(h['value']) for h in hist]
    # Collapse redundant series within each factor so one driver is not counted many times.
    kept = set(prune_correlated(usable, threshold=0.95)) if len(usable) > 1 else set(usable)
    for sid in list(usable):
        if sid not in kept:
            rejected.append({'series': sid, 'reason': 'redundant with a stronger series'})
            usable.pop(sid)
    return usable, rejected


def portfolio_vol(weights, vols, corr=None):
    """Correlation-aware volatility estimate (class priors; 0.5 for unrecognised tickers).

    This used to assume zero correlation, which reported an equity-only book as 10% volatility
    when it was really about 16%.
    """
    return _corr_vol(weights, vols, corr) if weights else 0.0


def _history_values(c, sid, at):
    meta = M.get(sid)
    if not meta:
        return []
    try:
        return [h['value'] for h in series_history(c, meta['feed'], sid, at=at)]
    except Exception:
        return []


def decide(c, home, asset_vols, prev_weights=None, equity_curve=None, run_history=None,
           target_vol=0.10, max_turnover=0.50, at=None, price_series=None, quadrant_tilt=0.5,
           corr=None, max_dd=0.10):
    """Produce one decision dict from the journal as of ``at``. Safe with an empty database.

    * Sizing is volatility-targeted **with correlations** (``corr``: ``None`` = class priors).
    * The macro quadrant now **tilts the weights** (``quadrant_tilt``, 0 = off, 1 = follow the
      All-Weather table fully). It used to be reported but never used.
    * ``price_series`` (``{asset: [prices oldest first]}``) enables a trend filter: an asset
      below its 200-day average has its weight halved.
    * The regime uses the curve and SOFR *history* (re-steepening after an inversion, a 1 pp
      tightening), not only today's levels.
    * A data outage steps toward half of the current book inside the turnover budget, instead of
      liquidating everything with zero reported turnover.
    """
    d = at or now()
    usable, rejected = usable_inputs(c, d)
    zs = macro_z_scores(usable)

    # Signals for the regime gate use the same row the dashboard shows.
    signals = {}
    for sid in (CURVE_ID, SOFR_ID):
        meta = M.get(sid)
        if not meta:
            continue
        try:
            row = lens(c, meta['feed'], sid, d)
        except Exception:
            row = None
        if row and row.get('state') == 'accepted' and row.get('current_value') is not None:
            try:
                signals[sid] = Decimal(row['current_value'])
            except Exception:
                pass

    regime = detect_regime(signals, curve_history=_history_values(c, CURVE_ID, d),
                           sofr_history=_history_values(c, SOFR_ID, d))
    quadrant = classify_macro_quadrant(zs['growth_z'], zs['inflation_z'])
    diffusion = macro_diffusion(signals)
    chains = detect_macro_chains(signals, inflation_z=zs['inflation_z'])
    sentiment = sentiment_guard(None)
    rf = float(signals[SOFR_ID]) / 100.0 if SOFR_ID in signals else None

    tilts = quadrant_tilts(list(asset_vols or {}), quadrant, quadrant_tilt)
    trend = trend_tilts(price_series) if price_series else {}
    for a, t in trend.items():
        if a in tilts:
            tilts[a] *= t
    plan = allocate(asset_vols, target_vol=target_vol, risk_scale=regime['risk_scale'],
                    corr=corr, tilts=tilts)
    target = plan['weights']
    data_outage = not usable
    if data_outage:
        # No scoreable macro input is an unknown picture, not a neutral one. Neither keep full
        # risk on, nor dump the book: step toward half of it, inside the budget.
        target = outage_target(prev_weights) if prev_weights else {}
    if prev_weights is not None:
        booked, turnover, partial = apply_turnover_budget(target, prev_weights, max_turnover)
    else:
        booked, turnover, partial = target, 0.0, False

    guard_note = None
    # Report the drawdown itself, not only the guard's reaction: the supervisor's drawdown limit
    # reads this field. Kept as a positive fraction (0.08 = 8% below the rolling peak).
    has_curve = bool(equity_curve)
    dd = drawdown_state(equity_curve, max_dd=max_dd) if has_curve else None
    if dd and dd['triggered']:
        brk = drawdown_guard(booked, 1.0 - dd['drawdown'], 1.0, max_dd=max_dd)
        if brk != booked:
            booked, guard_note = brk, 'drawdown guard halved risky exposure'
    elif dd and dd['breached'] and dd['reentered']:
        guard_note = 'drawdown limit breached but NAV is back above its trend: full risk allowed'

    orders = generate_orders(prev_weights or {}, booked)
    budget = drift_check(turnover, max_turnover=max_turnover, missing_ratio=1.0 if data_outage else 0.0)

    confirmed = confirm_regime(run_history) if run_history else regime['regime']
    return {
        'asof_utc': iso(d),
        'regime': regime['regime'],
        'regime_confirmed': confirmed,
        'regime_changed_vs_history': bool(run_history) and confirmed != regime['regime'],
        'risk_scale': regime['risk_scale'],
        'regime_reason': regime.get('reason'),
        'quadrant': quadrant,
        'growth_z': zs['growth_z'],
        'inflation_z': zs['inflation_z'],
        'z_sources': {k: zs[k] for k in ('growth_sources', 'inflation_sources')},
        'z_detail': {'growth': zs['growth_detail'], 'inflation': zs['inflation_detail'],
                     'growth_region_weights': zs.get('growth_region_weights'),
                     'inflation_region_weights': zs.get('inflation_region_weights'),
                     'converted_to_yoy': zs.get('converted_to_yoy')},
        'diffusion_pct': diffusion['diffusion_pct'],
        'chains': chains,
        'sentiment': sentiment,
        'tilts': {a: round(t, 4) for a, t in tilts.items()},
        'trend': trend,
        'target_weights': target,
        'booked_weights': booked,
        'cash': round(max(0.0, 1.0 - sum(booked.values())), 6),
        'turnover': round(turnover, 6),
        'turnover_one_way': round(turnover / 2.0, 6),
        'partial_rebalance': partial,
        'data_outage': data_outage,
        'outage_policy': ('step toward %.0f%% of current exposure inside the turnover budget'
                          % (100 * OUTAGE_RISK_SCALE)) if data_outage else None,
        'portfolio_vol_estimate': round(portfolio_vol(booked, asset_vols, corr), 4),
        'target_vol': target_vol,
        'risk_free_rate': round(rf, 6) if rf is not None else None,
        'all_weather_weights': all_weather_allocation(quadrant, regime['risk_scale']),
        'orders': orders,
        'budget_ok': budget['ok'],
        'budget_issues': budget['issues'],
        'guard': guard_note,
        # Present when a curve was offered, even if nothing in it was usable, so the supervisor can
        # tell 'not measured because none was given' from 'given but unusable'.
        **({'drawdown_pct': dd['drawdown']} if has_curve else {}),
        'inputs_used': sorted(usable),
        'inputs_rejected': rejected,
        'disclaimer': 'Decision support only. Observational macro data is not an executable price and not a return forecast.',
    }
