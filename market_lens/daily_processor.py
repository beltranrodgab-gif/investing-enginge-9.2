"""End-to-end daily/monthly processor: Filter -> Regime/Quadrant -> Sizing -> Paper-Trade Log.

Risk controls here are *budgets*, not kill switches. A breach produces a smaller step toward the
target and a recorded warning; it never silently empties the portfolio.
"""
import json
from decimal import Decimal
from pathlib import Path
from signal_filter import filter_signals, classify_macro_quadrant
from alpha_engine import detect_regime, allocate, apply_turnover_budget, drift_check, outage_target
from macro_alerts import macro_diffusion


# The quadrant table lives in ``asset_classes`` so every module uses one allocation.
from asset_classes import ALL_WEATHER_MAP  # noqa: E402  (re-exported)


def all_weather_allocation(quadrant, risk_scale=1.0):
    """Scale a quadrant's risk sleeves by ``risk_scale``; the difference is held in T-bills."""
    base = ALL_WEATHER_MAP.get(quadrant, ALL_WEATHER_MAP['unknown'])
    try:
        scale = max(0.0, min(1.0, float(risk_scale)))
    except (TypeError, ValueError):
        scale = 0.0
    scaled = {k: round(v * scale, 4) for k, v in base.items() if k != 'TBILL'}
    tbill = round(max(0.0, 1.0 - sum(scaled.values())), 4)
    if tbill > 0:
        scaled['TBILL'] = tbill
    return scaled


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float('inf') else None


def run_processor(csv_path, asset_vols, prev_weights=None, growth_z=None, inflation_z=None,
                  log_path=None, drift_threshold=0.05, at=None, target_vol=0.10, max_turnover=0.50):
    """Run one complete fail-closed processor cycle and optionally log the decision."""
    prev_weights = {k: float(v) for k, v in (prev_weights or {}).items()}
    filtered = filter_signals(csv_path, dedupe_groups=True, at=at)
    flat_signals = {k: v['value'] for k, v in filtered.items()}
    regime = detect_regime(flat_signals)
    quadrant = classify_macro_quadrant(growth_z, inflation_z)
    diffusion = macro_diffusion(flat_signals)
    plan = allocate(asset_vols, target_vol=target_vol, risk_scale=regime['risk_scale'])
    target_w = plan['weights']

    # Drift trigger: skip the trade only when the move is small *and* the budget is clear.
    max_drift = max((abs(target_w.get(a, 0.0) - prev_weights.get(a, 0.0))
                     for a in set(target_w) | set(prev_weights)), default=0.0)
    rebalance = max_drift >= drift_threshold or not prev_weights
    wanted = target_w if rebalance else dict(prev_weights)

    # Budget, not kill switch: a switch larger than the budget is taken in instalments.
    if not filtered:
        # Data outage: step toward half of the current book inside the budget (see
        # ``alpha_engine.outage_target``) instead of liquidating everything for free.
        final_w, turnover, partial = apply_turnover_budget(outage_target(prev_weights), prev_weights,
                                                           max_turnover)
        warnings = ['no fresh screened signals: data outage, stepping toward half of current risk']
        budget = drift_check(turnover, max_turnover=max_turnover, missing_ratio=1.0)
    else:
        final_w, turnover, partial = apply_turnover_budget(wanted, prev_weights, max_turnover)
        budget = drift_check(turnover, max_turnover=max_turnover, missing_ratio=0.0)
        warnings = []
        if partial:
            warnings.append(f'turnover budget applied: moved {turnover:.2%}, booked over {max_turnover:.2%} in one step')
    record = {
        'regime': regime['regime'],
        'risk_scale': regime['risk_scale'],
        'regime_reason': regime.get('reason'),
        'quadrant': quadrant,
        'diffusion_pct': diffusion['diffusion_pct'],
        'rebalance_triggered': rebalance,
        'partial_rebalance': partial,
        'turnover': round(turnover, 6),
        'max_drift': round(max_drift, 6),
        'weights': final_w,
        'cash': round(max(0.0, 1.0 - sum(final_w.values())), 6),
        'all_weather_weights': all_weather_allocation(quadrant, regime['risk_scale']) if filtered else {},
        'drift_ok': budget['ok'],
        'data_outage': not filtered,
        'warnings': warnings,
        'filtered_count': len(filtered),
    }
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        with Path(log_path).open('a', encoding='utf-8') as f:
            f.write(json.dumps(record) + '\n')
    return record


def confirm_regime(recent_regimes, confirm_days=2):
    """Hysteresis gate: switch only after ``confirm_days`` identical new readings.

    Returns the confirmed regime, which is the *current* one when the last ``confirm_days``
    readings agree, otherwise the regime in force before the unconfirmed readings began.
    """
    if not recent_regimes:
        return 'uncertain'
    n = max(1, int(confirm_days))
    if len(recent_regimes) >= n and len(set(recent_regimes[-n:])) == 1:
        return recent_regimes[-1]
    # Find where the trailing run of identical values starts; the previous value is the held regime.
    last = recent_regimes[-1]
    i = len(recent_regimes) - 1
    while i >= 0 and recent_regimes[i] == last:
        i -= 1
    return recent_regimes[i] if i >= 0 else last


def drawdown_guard(weights, current_nav, peak_nav, max_dd=0.10):
    """Circuit breaker: halve risky weights into TBILL if drawdown exceeds max_dd."""
    try:
        cur, peak = float(current_nav), float(peak_nav)
    except (TypeError, ValueError):
        return weights
    if peak <= 0 or cur < 0 or (cur / peak - 1.0) > -max_dd:
        return weights
    scaled = {k: round(v * 0.5, 4) for k, v in weights.items() if k != 'TBILL'}
    scaled['TBILL'] = round(max(0.0, 1.0 - sum(scaled.values())), 4)
    return scaled


def bayesian_regime_update(prior_probs, likelihoods, floor=0.05):
    """Bayesian posterior regime probabilities: P(R|x) proportional to P(x|R) * P_prior(R)."""
    unnorm = {r: max(floor, prior_probs.get(r, 0.25)) * max(1e-6, likelihoods.get(r, 0.25))
              for r in set(prior_probs) | set(likelihoods)}
    total = sum(unnorm.values())
    return {r: round(v / total, 6) for r, v in unnorm.items()}


def drawdown_state(equity_curve, max_dd=0.10, peak_window=252, reentry_window=50):
    """Drawdown measured against a *rolling* peak, with a trend-based re-entry rule.

    The old guard compared NAV with its all-time high and halved risk whenever it was 10% below.
    At half risk, getting back within 10% of that high takes far longer, so the guard could stay
    on indefinitely. Now:

    * the peak is the highest NAV within the last ``peak_window`` observations, so a very old high
      eventually stops defining the drawdown;
    * once breached, the guard switches off again (``reentered``) when NAV is back above its
      ``reentry_window``-observation moving average, i.e. a recovery is under way.

    Returns ``{'drawdown', 'peak', 'breached', 'reentered', 'triggered'}``; ``triggered`` is the
    one to act on. Non-finite or non-positive points are skipped.
    """
    curve = []
    for x in equity_curve or []:
        try:
            v = float(x)
        except (TypeError, ValueError):
            continue
        if v == v and abs(v) != float('inf') and v > 0:
            curve.append(v)
    if not curve:
        return {'drawdown': None, 'peak': None, 'breached': False, 'reentered': False,
                'triggered': False}
    window = curve[-max(1, int(peak_window)):]
    peak = max(window)
    cur = curve[-1]
    dd = max(0.0, 1.0 - cur / peak)
    breached = dd >= float(max_dd)
    reentered = False
    n = max(2, int(reentry_window))
    if breached and len(curve) >= n:
        sma = sum(curve[-n:]) / n
        reentered = cur > sma
    return {'drawdown': round(dd, 6), 'peak': peak, 'breached': breached,
            'reentered': reentered, 'triggered': breached and not reentered}
