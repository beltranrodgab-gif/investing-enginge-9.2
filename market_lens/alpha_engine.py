"""Alpha engine: regime detection, macro-surprise scoring, volatility-targeted sizing, and drift checks."""
import math
from decimal import Decimal, InvalidOperation
from strategy_adapter import load_screened_signals


def _finite(x):
    """True only for real, finite, positive numbers. Rejects None, NaN, inf and strings that look numeric."""
    if isinstance(x, bool) or x is None:
        return False
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0.0


def _decimal(x):
    try:
        d = Decimal(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def detect_regime(signals):
    """Classify macro/liquidity regime from fail-closed screened signals.

    Every input that the classification depends on must be present and sane. A missing or
    out-of-range SOFR is not 'normal expansion': it is an unknown funding environment, so the
    result is fail-closed at half risk.
    """
    curve = _decimal(signals.get('UST.DAILY.10Y2Y'))
    sofr = _decimal(signals.get('NYFED.SOFR'))
    if curve is None:
        return {'regime': 'uncertain', 'risk_scale': 0.5, 'reason': 'missing UST.DAILY.10Y2Y'}
    if not (Decimal('-25') <= curve <= Decimal('25')):
        return {'regime': 'uncertain', 'risk_scale': 0.5, 'reason': f'implausible 10Y-2Y={curve}'}
    if sofr is None:
        return {'regime': 'uncertain', 'risk_scale': 0.5, 'reason': f'10Y-2Y={curve}; missing NYFED.SOFR'}
    if not (Decimal('0') <= sofr <= Decimal('30')):
        return {'regime': 'uncertain', 'risk_scale': 0.5, 'reason': f'implausible SOFR={sofr}'}
    if curve < Decimal('0'):
        return {'regime': 'defensive_inverted_curve', 'risk_scale': 0.5, 'reason': f'10Y-2Y={curve}'}
    if sofr > Decimal('5.5'):
        return {'regime': 'tight_liquidity', 'risk_scale': 0.75, 'reason': f'SOFR={sofr}'}
    return {'regime': 'normal_expansion', 'risk_scale': 1.0, 'reason': f'10Y-2Y={curve}, SOFR={sofr}'}


def _cap_normalised(raw, max_weight):
    """Water-filling cap so that every weight is <= max_weight and the survivors stay proportional.

    Returns (weights, unallocated). Weights sum to 1 - unallocated. unallocated is the part that
    cannot be placed because every remaining sleeve is at its cap (n * cap < 1).
    """
    w = dict(raw)
    free = set(w)
    for _ in range(64):
        over = {k for k in free if w[k] > max_weight + 1e-12}
        if not over:
            break
        excess = sum(w[k] - max_weight for k in over)
        for k in over:
            w[k] = max_weight
        free -= over
        total = sum(w[k] for k in free)
        if not free or total <= 1e-12:
            break
        for k in free:
            w[k] += excess * (w[k] / total)
    total = sum(w.values())
    if total > 1.0 + 1e-12:  # numerical drift only; caps cannot push above one
        w = {k: v / total for k, v in w.items()}
        total = 1.0
    return w, max(0.0, 1.0 - total)


def allocate(vols, target_vol=0.10, risk_scale=1.0, max_weight=0.40):
    """Volatility-targeted inverse-volatility allocation with a per-asset cap and explicit cash.

    The portfolio is sized to ``target_vol`` on the assumption of zero cross-correlation, and can
    never take more than ``risk_scale`` of full exposure. Whatever is left after the cap is
    returned as ``cash`` rather than being silently dropped.
    """
    valid = {k: float(v) for k, v in vols.items() if _finite(v)}
    if not valid:
        return {'weights': {}, 'cash': 1.0, 'exposure': 0.0, 'base_vol': None, 'cap': max_weight}
    if not _finite(max_weight) or max_weight <= 0:
        raise ValueError('max_weight must be a positive number')
    max_weight = min(float(max_weight), 1.0)
    inv = {k: 1.0 / v for k, v in valid.items()}
    total = sum(inv.values())
    base = {k: v / total for k, v in inv.items()}
    # ``capped`` already sums to 1 - unallocated (the cap has been applied). Scaling it by the
    # unallocated part again applied the cap twice and left a single-asset book at 16% under a
    # 40% cap.
    capped, cap_unallocated = _cap_normalised(base, max_weight)
    # Zero-correlation portfolio volatility of the (capped) base portfolio.
    base_vol = math.sqrt(sum((capped[k] * valid[k]) ** 2 for k in capped))
    try:
        exposure = float(risk_scale)
    except (TypeError, ValueError):
        exposure = 0.0
    exposure = max(0.0, min(1.0, exposure)) if math.isfinite(exposure) else 0.0
    if _finite(target_vol) and base_vol > 0:
        exposure = min(exposure, float(target_vol) / base_vol)
    weights = {k: capped[k] * exposure for k in capped}
    cash = max(0.0, 1.0 - sum(weights.values()))
    return {'weights': weights, 'cash': cash, 'exposure': exposure, 'base_vol': base_vol,
            'cap': max_weight, 'cap_unallocated': cap_unallocated}


def inverse_vol_weights(vols, target_vol=0.10, risk_scale=1.0, max_weight=0.40):
    """Volatility-targeted inverse-volatility portfolio weights with per-asset cap.

    Weights never exceed ``max_weight`` and sum to at most ``risk_scale``. The remainder is cash;
    it is not redistributed to the survivors, so the cap cannot be broken.
    """
    return allocate(vols, target_vol, risk_scale, max_weight)['weights']


def drift_check(realized_turnover, max_turnover=0.50, missing_ratio=0.0, max_missing=0.20):
    """Fail-safe drift & data-completeness monitor.

    This is a *monitor*. It reports whether a rebalance breached a budget; it must never be used
    to liquidate a portfolio, because the breaching trade is exactly the one that needs doing.
    """
    issues = []
    if realized_turnover > max_turnover:
        issues.append(f'turnover {realized_turnover:.2%} > {max_turnover:.2%}')
    if missing_ratio > max_missing:
        issues.append(f'missing {missing_ratio:.2%} > {max_missing:.2%}')
    return {'ok': not issues, 'issues': issues}


def apply_turnover_budget(target, prev, max_turnover=0.50):
    """Move toward ``target`` by at most ``max_turnover`` of portfolio value, never liquidating.

    A full model switch (e.g. all cash -> 80% invested) exceeds a 50% budget. The correct response
    is a partial rebalance that leaves the rest in cash and continues next period, not an empty
    book. Trades are scaled by a single factor so the proportions of the target are preserved.
    """
    prev = {k: float(v) for k, v in (prev or {}).items() if _finite_value(v)}
    target = {k: float(v) for k, v in (target or {}).items() if _finite_value(v)}
    if not target:
        return {}, 0.0, False
    distance = sum(abs(target.get(k, 0.0) - prev.get(k, 0.0)) for k in set(target) | set(prev))
    if distance <= max_turnover or distance <= 0:
        return dict(target), distance, False
    ratio = max_turnover / distance
    moved = {k: prev.get(k, 0.0) + (target.get(k, 0.0) - prev.get(k, 0.0)) * ratio for k in set(target) | set(prev)}
    moved = {k: v for k, v in moved.items() if v > 1e-12}
    total = sum(moved.values())
    if total > 1.0:  # never lever; scale back to a fully invested book
        moved = {k: v / total for k, v in moved.items()}
    return moved, max_turnover, True


def _finite_value(x):
    try:
        return math.isfinite(float(x)) and float(x) >= 0.0
    except (TypeError, ValueError):
        return False


def run_alpha_step(csv_path, asset_vols, realized_turnover=0.0, at=None, prev_weights=None,
                   target_vol=0.10, max_turnover=0.50):
    """End-to-end fail-closed alpha step from screened CSV to target weights.

    A missing, empty or stale export yields empty weights *and* an explicit reason; it never
    raises past the caller and never returns a partially built book.
    """
    signals = load_screened_signals(csv_path, at=at)
    reg = detect_regime(signals)
    drift = drift_check(realized_turnover, max_turnover=max_turnover,
                        missing_ratio=0.0 if signals else 1.0)
    if not signals:
        return {'regime': reg, 'weights': {}, 'drift': drift, 'signal_count': 0,
                'cash': 1.0, 'reason': 'no fresh screened signals'}
    if not drift['ok'] and drift['issues'] and all('missing' in i for i in drift['issues']):
        return {'regime': reg, 'weights': {}, 'drift': drift, 'signal_count': len(signals),
                'cash': 1.0, 'reason': 'data completeness below minimum'}
    plan = allocate(asset_vols, target_vol=target_vol, risk_scale=reg['risk_scale'])
    weights, turnover, partial = (plan['weights'], realized_turnover, False)
    if prev_weights is not None:
        weights, turnover, partial = apply_turnover_budget(plan['weights'], prev_weights, max_turnover)
    return {'regime': reg, 'weights': weights, 'drift': drift, 'signal_count': len(signals),
            'cash': max(0.0, 1.0 - sum(weights.values())), 'turnover': turnover,
            'partial_rebalance': partial}


def kelly_fraction(expected_excess_return, variance, fraction=0.5, max_weight=0.40):
    """Fractional Kelly Criterion: f* = fraction * (mu / sigma^2), capped for safety."""
    try:
        mu = float(expected_excess_return)
        var = float(variance)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(mu) or not math.isfinite(var) or var <= 1e-9 or mu <= 0:
        return 0.0
    raw = fraction * (mu / var)
    return round(min(max_weight, max(0.0, raw)), 4)


def kelly_detail(expected_return, volatility, fraction=0.5, max_weight=0.40, risk_free=0.0):
    """Kelly with the raw, uncapped and capped sizes exposed separately.

    ``half_kelly_position_size`` returning only the capped number made every ordinary input look
    identical (the cap binds). Callers that want to see the actual edge use this.
    """
    try:
        mu = float(expected_return) - float(risk_free)
        vol = float(volatility)
    except (TypeError, ValueError):
        return {'raw': 0.0, 'capped': 0.0, 'capped_by': 'invalid input'}
    if not math.isfinite(mu) or not math.isfinite(vol) or vol <= 0:
        return {'raw': 0.0, 'capped': 0.0, 'capped_by': 'invalid input'}
    raw = max(0.0, fraction * (mu / (vol ** 2)))
    capped = round(min(max_weight, raw), 4)
    return {'raw': round(raw, 4), 'capped': capped,
            'capped_by': 'max_weight' if raw > max_weight else ('edge' if raw > 0 else 'no edge')}


def find_historical_analogs(current_vec, history, top_k=3):
    """Find top-k most similar historical macro states using Cosine Similarity & Euclidean Distance."""
    import statistics
    if not current_vec or not history:
        return {'analogs': [], 'expected_forward_return': None}
    try:
        cur = [float(x) for x in current_vec]
    except (TypeError, ValueError):
        return {'analogs': [], 'expected_forward_return': None}
    if not all(math.isfinite(x) for x in cur):
        return {'analogs': [], 'expected_forward_return': None}
    norm_c = math.sqrt(sum(x * x for x in cur))
    scored = []
    for item in history:
        hv = item.get('vector') or []
        fwd = item.get('forward_return')
        if len(hv) != len(cur) or fwd is None:
            continue
        try:
            vec = [float(x) for x in hv]
            fwd = float(fwd)
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(x) for x in vec) or not math.isfinite(fwd):
            continue
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(cur, vec)))
        norm_h = math.sqrt(sum(x * x for x in vec))
        cos = sum(a * b for a, b in zip(cur, vec)) / (norm_c * norm_h) if norm_c > 0 and norm_h > 0 else 0.0
        scored.append({'date': item.get('date', ''), 'distance': round(dist, 4),
                       'cosine_sim': round(cos, 4), 'forward_return': fwd})
    scored.sort(key=lambda x: (-x['cosine_sim'], x['distance']))
    top = scored[:max(1, int(top_k))]
    rets = [x['forward_return'] for x in top]
    return {'analogs': top, 'expected_forward_return': round(statistics.mean(rets), 6) if rets else None}


def sentiment_guard(market_z):
    """Behavioral finance gate: dampen herding at extreme euphoria (> +2.0z) and prevent panic selling (< -2.0z)."""
    if market_z is None:
        return {'sentiment': 'neutral', 'risk_multiplier': 1.0}
    try:
        z = float(market_z)
    except (TypeError, ValueError):
        return {'sentiment': 'neutral', 'risk_multiplier': 1.0}
    if not math.isfinite(z):
        return {'sentiment': 'neutral', 'risk_multiplier': 1.0}
    if z >= 2.0:
        return {'sentiment': 'extreme_euphoria', 'risk_multiplier': 0.75}
    if z <= -2.0:
        return {'sentiment': 'extreme_panic', 'risk_multiplier': 0.85}
    return {'sentiment': 'normal', 'risk_multiplier': 1.0}
