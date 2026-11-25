"""Bridge from the collected journal to a strategy decision.

The strategy modules were previously a library with no caller: ``market_lens.py`` never imported
them, ``growth_z``/``inflation_z`` had no data source, and no command produced a portfolio. This
module assembles the inputs that already exist in SQLite (accepted, point-in-time history), runs the
filters, and returns one explainable decision.

Everything here is decision support, not an execution feed. It never contacts a broker.
"""
import math
import statistics
from decimal import Decimal

from sources import F, M, VERS
from store import now, iso, db, lens, ready_row, read_cells, feed_state
from reliability import beat_info, quality, FALLBACKS
from signal_filter import (macro_z_scores, classify_macro_quadrant, revision_noise_ok,
                           prune_correlated, history_zscore)
from alpha_engine import detect_regime, allocate, apply_turnover_budget, drift_check, sentiment_guard
from daily_processor import all_weather_allocation, confirm_regime, drawdown_guard
from macro_alerts import macro_diffusion, detect_macro_chains
from trade_signals import generate_orders


def series_history(c, feed, series_id, limit=300):
    """Chronological accepted values for one series, ascending by reference period (PIT by receipt)."""
    r = ready_row(c, feed, iso(now()))
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


def usable_inputs(c):
    """Series with enough clean history to be scored, plus what had to be rejected."""
    usable, rejected = {}, []
    for sid, meta in M.items():
        if meta.get('factor') not in ('ACT-01', 'LAB-02', 'YLD-03', 'INF-01', 'INF-03', 'COM-02'):
            continue
        k = meta['feed']
        try:
            p, last, state = feed_state(c, k, now())
        except Exception:
            rejected.append({'series': sid, 'reason': 'journal unavailable'})
            continue
        if not last or state not in ('accepted', 'overdue'):
            rejected.append({'series': sid, 'reason': f'feed {state}'})
            continue
        if not revision_noise_ok(c, sid):
            rejected.append({'series': sid, 'reason': 'excessive revisions'})
            continue
        hist = series_history(c, k, sid)
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


def portfolio_vol(weights, vols):
    """Zero-correlation volatility estimate. Understates a correlated book; stated, not hidden."""
    return (sum((w * vols.get(a, 0.0)) ** 2 for a, w in weights.items())) ** 0.5 if weights else 0.0


def decide(c, home, asset_vols, prev_weights=None, equity_curve=None, run_history=None,
           target_vol=0.10, max_turnover=0.50, at=None):
    """Produce one decision dict from the live journal. Safe to call with an empty database."""
    d = at or now()
    usable, rejected = usable_inputs(c)
    zs = macro_z_scores(usable)

    # Signals for the regime gate use the same row the dashboard shows.
    signals = {}
    for sid in ('UST.DAILY.10Y2Y', 'NYFED.SOFR'):
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

    regime = detect_regime(signals)
    quadrant = classify_macro_quadrant(zs['growth_z'], zs['inflation_z'])
    diffusion = macro_diffusion(signals)
    chains = detect_macro_chains(signals, inflation_z=zs['inflation_z'])
    sentiment = sentiment_guard(None)

    plan = allocate(asset_vols, target_vol=target_vol, risk_scale=regime['risk_scale'])
    target = plan['weights']
    if not usable:
        # No scoreable macro input is not a neutral macro picture: it is an unknown one. Holding a
        # fully sized book on zero evidence is exactly how a data outage becomes a position.
        target = {}
    if prev_weights is not None:
        booked, turnover, partial = apply_turnover_budget(target, prev_weights, max_turnover)
    else:
        booked, turnover, partial = target, 0.0, False
    guard_note = None
    # Report the drawdown itself, not only the guard's reaction: the supervisor's drawdown limit
    # reads this field, and without it that check could never fire. Kept as a positive fraction
    # (0.08 = 8% below the peak) so it compares directly with a limit.
    drawdown_pct = None
    has_curve = bool(equity_curve)
    if equity_curve:
        curve = []
        for x in equity_curve:
            try:
                v = float(x)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v) and v > 0:
                curve.append(v)
        if curve:
            peak = max(curve)
            drawdown_pct = round(max(0.0, 1.0 - curve[-1] / peak), 6) if peak > 0 else None
        # The guard gets the *cleaned* curve. It used to receive the raw one, so a single
        # non-numeric point made max(equity_curve) raise a TypeError and the whole decision fail —
        # a bad log row took down the strategy rather than being skipped.
        if len(curve) >= 2:
            brk = drawdown_guard(booked, curve[-1], max(curve), max_dd=0.10)
            if brk != booked:
                booked, guard_note = brk, 'drawdown guard halved risky exposure'
    orders = generate_orders(prev_weights or {}, booked)
    budget = drift_check(turnover, max_turnover=max_turnover, missing_ratio=0.0 if usable else 1.0)

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
        'z_detail': {'growth': zs['growth_detail'], 'inflation': zs['inflation_detail']},
        'diffusion_pct': diffusion['diffusion_pct'],
        'chains': chains,
        'sentiment': sentiment,
        'target_weights': target,
        'booked_weights': booked,
        'cash': round(max(0.0, 1.0 - sum(booked.values())), 6),
        'turnover': round(turnover, 6),
        'partial_rebalance': partial,
        'portfolio_vol_estimate': round(portfolio_vol(booked, asset_vols), 4),
        'target_vol': target_vol,
        'all_weather_weights': all_weather_allocation(quadrant, regime['risk_scale']),
        'orders': orders,
        'budget_ok': budget['ok'],
        'budget_issues': budget['issues'],
        'guard': guard_note,
        # Present when a curve was offered, even if nothing in it was usable, so the supervisor can
        # tell 'not measured because none was given' from 'given but unusable'.
        **({'drawdown_pct': drawdown_pct} if has_curve else {}),
        'inputs_used': sorted(usable),
        'inputs_rejected': rejected,
        'disclaimer': 'Decision support only. Observational macro data is not an executable price and not a return forecast.',
    }
