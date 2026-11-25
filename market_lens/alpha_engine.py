"""Alpha engine: regime detection, macro-surprise scoring, volatility-targeted sizing, and drift checks."""
import math
from decimal import Decimal, InvalidOperation
from strategy_adapter import load_screened_signals
import asset_classes


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


def _hist(values):
    out = []
    for x in values or []:
        d = _decimal(x)
        if d is not None:
            out.append(d)
    return out


def detect_regime(signals, curve_history=None, sofr_history=None, curve_lookback=250,
                  sofr_lookback=126, steepening=Decimal('0.25'), tightening=Decimal('1.0')):
    """Classify macro/liquidity regime from fail-closed screened signals.

    Every input that the classification depends on must be present and sane. A missing or
    out-of-range SOFR is not 'normal expansion': it is an unknown funding environment, so the
    result is fail-closed at half risk.

    The rules use the *dynamics* of the curve and of funding, not only their levels:

    * An inverted curve is an early warning (historically 6-24 months ahead of a recession, and
      equities often rose through it), so it trims risk to 75% instead of halving it.
    * The dangerous phase is the **re-steepening after an inversion**: recessions have tended to
      start once the curve un-inverts. With ``curve_history`` (oldest first), a curve that is now
      non-negative, was inverted within ``curve_lookback`` observations and has risen by at least
      ``steepening`` from that low is classified ``post_inversion_steepening`` at half risk.
    * Tight liquidity is a *tightening*, not a fixed level: with ``sofr_history`` a rise of
      ``tightening`` (1 pp) or more over ``sofr_lookback`` observations is ``tight_liquidity``.
      Without history the legacy absolute rule (SOFR > 5.5%) is used as a fallback.
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
        return {'regime': 'defensive_inverted_curve', 'risk_scale': 0.75,
                'reason': f'10Y-2Y={curve} (inverted: early warning)'}
    ch = _hist(curve_history)[-int(curve_lookback):]
    if ch:
        low = min(ch)
        if low < Decimal('0') and curve - low >= steepening:
            return {'regime': 'post_inversion_steepening', 'risk_scale': 0.5,
                    'reason': f'10Y-2Y={curve}, re-steepened {curve - low} pp from {low} inside the lookback'}
    sh = _hist(sofr_history)[-int(sofr_lookback):]
    if len(sh) >= max(2, int(sofr_lookback) // 2):
        rise = sofr - min(sh)
        if rise >= tightening:
            return {'regime': 'tight_liquidity', 'risk_scale': 0.75,
                    'reason': f'SOFR={sofr}, up {rise} pp inside the lookback'}
    elif sofr > Decimal('5.5'):
        return {'regime': 'tight_liquidity', 'risk_scale': 0.75, 'reason': f'SOFR={sofr} (no history; level rule)'}
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


def allocate(vols, target_vol=0.10, risk_scale=1.0, max_weight=0.40, corr=None, tilts=None):
    """Volatility-targeted inverse-volatility allocation with a per-asset cap and explicit cash.

    The portfolio is sized to ``target_vol`` **including correlations** and can never take more
    than ``risk_scale`` of full exposure. Whatever is left after the cap is returned as ``cash``.

    ``corr`` is ``None`` (class priors from ``asset_classes``, 0.5 for unknown tickers), one number
    for every pair, or a callable ``corr(a, b)``. Pass ``corr=0.0`` to reproduce the old
    zero-correlation sizing, which understated an equity-only book's risk by about 1.6x.

    ``tilts`` is an optional ``{asset: multiplier}`` applied to the inverse-volatility weights
    before capping (used for the macro-quadrant and trend tilts). A multiplier of 0 removes an
    asset.
    """
    valid = {k: float(v) for k, v in vols.items() if _finite(v)}
    if not valid:
        return {'weights': {}, 'cash': 1.0, 'exposure': 0.0, 'base_vol': None, 'cap': max_weight}
    if not _finite(max_weight) or max_weight <= 0:
        raise ValueError('max_weight must be a positive number')
    max_weight = min(float(max_weight), 1.0)
    inv = {}
    for k, v in valid.items():
        t = 1.0
        if tilts and k in tilts:
            try:
                t = float(tilts[k])
            except (TypeError, ValueError):
                t = 1.0
            t = max(0.0, t) if math.isfinite(t) else 1.0
        if t > 0:
            inv[k] = t / v
    if not inv:
        return {'weights': {}, 'cash': 1.0, 'exposure': 0.0, 'base_vol': None, 'cap': max_weight}
    total = sum(inv.values())
    base = {k: v / total for k, v in inv.items()}
    # ``capped`` already sums to 1 - unallocated (the cap has been applied). Scaling it by the
    # unallocated part again applied the cap twice and left a single-asset book at 16% under a
    # 40% cap.
    capped, cap_unallocated = _cap_normalised(base, max_weight)
    base_vol = asset_classes.portfolio_vol(capped, valid, corr)
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
            'cap': max_weight, 'cap_unallocated': cap_unallocated,
            'vol_estimate': asset_classes.portfolio_vol(weights, valid, corr)}


def portfolio_vol(weights, vols, corr=None):
    """Correlation-aware portfolio volatility (see ``asset_classes.portfolio_vol``)."""
    return asset_classes.portfolio_vol(weights, vols, corr)


def trend_tilts(price_series, window=200, min_obs=100, below=0.5):
    """Trend filter: assets trading below their moving average get their weight multiplied by
    ``below``. Assets with too little history get 1.0 (no view). Prices are oldest first."""
    out = {}
    for asset, px in (price_series or {}).items():
        clean = []
        for p in px or []:
            try:
                v = float(p)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v) and v > 0:
                clean.append(v)
        if len(clean) < int(min_obs):
            out[asset] = 1.0
            continue
        tail = clean[-int(window):]
        sma = sum(tail) / len(tail)
        out[asset] = float(below) if clean[-1] < sma else 1.0
    return out


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

    **Turnover is two-sided**: it is ``sum(|target_i - prev_i|)``, i.e. buys plus sells. Switching
    10% of the book from A to B therefore uses 0.20 of the budget, so ``max_turnover=0.50`` moves
    at most 25% of the book from one asset to another per step. Halve the two-sided figure for the
    one-way number brokers usually quote.

    A full model switch (e.g. all cash -> 80% invested) exceeds a 50% budget. The correct response
    is a partial rebalance that leaves the rest in cash and continues next period, not an empty
    book. Trades are scaled by a single factor so the proportions of the target are preserved.

    An *empty* target with an existing book is a move to cash and is budgeted like any other move.
    It used to return ``({}, 0.0, False)``: a complete liquidation reported as zero turnover,
    outside the budget and free of cost.
    """
    prev = {k: float(v) for k, v in (prev or {}).items() if _finite_value(v)}
    target = {k: float(v) for k, v in (target or {}).items() if _finite_value(v)}
    if not target and not prev:
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


# Data-outage policy. With no usable evidence the strategy neither keeps full risk on (a feed
# outage must not be read as "all clear") nor dumps the book in one go (a data glitch must not
# become a costed, untracked fire sale at whatever price the market offers). It steps toward
# half of the current exposure -- the same 0.5 used for an 'uncertain' regime -- inside the normal
# turnover budget, and says so.
OUTAGE_RISK_SCALE = 0.5


def outage_target(current_weights, scale=OUTAGE_RISK_SCALE):
    """Target book during a data outage: current positions scaled by ``scale``."""
    out = {}
    for k, v in (current_weights or {}).items():
        if _finite_value(v) and float(v) > 0:
            out[k] = float(v) * float(scale)
    return out


def run_alpha_step(csv_path, asset_vols, realized_turnover=0.0, at=None, prev_weights=None,
                   target_vol=0.10, max_turnover=0.50):
    """End-to-end fail-closed alpha step from screened CSV to target weights.

    A missing, empty or stale export is a **data outage**: the result carries ``data_outage``,
    an explicit ``reason`` and, when a book is held, a budgeted step toward half of it
    (``outage_target``). It never raises past the caller and never sells everything at once.
    """
    signals = load_screened_signals(csv_path, at=at)
    reg = detect_regime(signals)
    drift = drift_check(realized_turnover, max_turnover=max_turnover,
                        missing_ratio=0.0 if signals else 1.0)

    def outage(reason):
        if prev_weights:
            w, t, partial = apply_turnover_budget(outage_target(prev_weights), prev_weights, max_turnover)
        else:
            w, t, partial = {}, 0.0, False
        return {'regime': reg, 'weights': w, 'drift': drift, 'signal_count': len(signals),
                'cash': max(0.0, 1.0 - sum(w.values())), 'turnover': t,
                'partial_rebalance': partial, 'data_outage': True, 'reason': reason}

    if not signals:
        return outage('no fresh screened signals')
    if not drift['ok'] and drift['issues'] and all('missing' in i for i in drift['issues']):
        return outage('data completeness below minimum')
    plan = allocate(asset_vols, target_vol=target_vol, risk_scale=reg['risk_scale'])
    weights, turnover, partial = (plan['weights'], realized_turnover, False)
    if prev_weights is not None:
        weights, turnover, partial = apply_turnover_budget(plan['weights'], prev_weights, max_turnover)
    return {'regime': reg, 'weights': weights, 'drift': drift, 'signal_count': len(signals),
            'cash': max(0.0, 1.0 - sum(weights.values())), 'turnover': turnover,
            'partial_rebalance': partial, 'data_outage': False}


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
