"""Macro-to-Micro Stock & Sector Analyzer: EV/EBITDA, ROIC, Altman Z, Monte Carlo, and Kelly.

Design rules enforced here:

* A screen only passes an asset when the data it needs is actually present. Missing fundamentals
  are a rejection, not a default that slips through.
* A cap is a cap. Weights are capped and the remainder is returned as cash; renormalising must
  never push a sleeve back above its limit.
* Risk numbers are deterministic for a given input. A "99% VaR" is a quantile, not one random draw.
"""
import math, random, statistics
from signal_filter import classify_macro_quadrant
from alpha_engine import kelly_fraction, kelly_detail

SECTOR_MAP = {
    'rising_growth_falling_inflation': ['SPY', 'QQQ', 'XLK', 'XLY'],
    'rising_growth_rising_inflation':  ['XLE', 'XLB', 'XLI', 'GLD'],
    'falling_growth_rising_inflation': ['XLP', 'XLU', 'XLV', 'GLD'],
    'falling_growth_falling_inflation':['TLT', 'IEF', 'XLV', 'XLP'],
    'unknown':                         ['SGOV', 'BIL'],
}

NORMAL_99 = 2.3263478740408408  # one-sided 99% standard-normal quantile


def _num(x):
    """Return a finite float or None. Rejects bools, None, NaN, inf and non-numeric strings."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _water_fill(raw, caps):
    """Cap each key at its limit and spread the excess over keys that still have room.

    ``raw`` should sum to <= 1. Any amount that cannot be placed (every sleeve at its cap) is
    returned as the residual instead of being pushed into a capped sleeve. Caps always hold.
    """
    w = {k: max(0.0, float(v)) for k, v in raw.items()}
    total = sum(w.values())
    if total > 1.0:
        w = {k: v / total for k, v in w.items()}
        total = 1.0
    for _ in range(64):
        over = [k for k in w if w[k] > caps[k] + 1e-12]
        if not over:
            break
        excess = sum(w[k] - caps[k] for k in over)
        for k in over:
            w[k] = caps[k]
        room = {k: caps[k] - w[k] for k in w if caps[k] - w[k] > 1e-12}
        pool = sum(room.values())
        if pool <= 1e-12:
            break
        for k, r in room.items():
            w[k] += excess * (r / pool)
    total = sum(w.values())
    if total > 1.0 + 1e-12:
        w = {k: v / total for k, v in w.items()}
        total = 1.0
    return w, max(0.0, 1.0 - total)


def recommend_stocks(growth_z, inflation_z, risk_scale=1.0):
    quad = classify_macro_quadrant(growth_z, inflation_z)
    return {'quadrant': quad, 'risk_scale': risk_scale, 'recommended_etfs': SECTOR_MAP.get(quad, SECTOR_MAP['unknown'])}


def score_stock_fundamentals(stocks, max_debt_eq=2.5, max_ev_ebitda=20.0, min_roic=0.10, min_altman_z=1.8):
    """Filter & rank stocks by FCF yield, earnings growth, EV/EBITDA, ROIC, Altman Z, and leverage.

    Every ratio used for a decision must be present. Previously a missing ratio silently became a
    comfortable default (ROIC 0.15, Altman 3.0) and the stock passed the screen.
    """
    passed = []
    rejected = []
    required = ('fcf_yield', 'earnings_growth', 'debt_to_equity', 'ev_ebitda', 'roic', 'altman_z')
    for s in stocks:
        missing = [k for k in required if _num(s.get(k)) is None]
        if missing or not s.get('ticker'):
            rejected.append({'ticker': s.get('ticker', '?'), 'reason': 'missing ' + ', '.join(missing or ['ticker'])})
            continue
        val = {k: _num(s[k]) for k in required}
        if val['debt_to_equity'] > max_debt_eq or val['fcf_yield'] <= 0:
            rejected.append({'ticker': s['ticker'], 'reason': 'leverage or negative FCF yield'})
            continue
        if val['ev_ebitda'] <= 0 or val['ev_ebitda'] > max_ev_ebitda:
            rejected.append({'ticker': s['ticker'], 'reason': 'EV/EBITDA outside range'})
            continue
        if val['roic'] < min_roic or val['altman_z'] < min_altman_z:
            rejected.append({'ticker': s['ticker'], 'reason': 'ROIC or Altman Z below floor'})
            continue
        score = round(0.4 * val['fcf_yield'] + 0.3 * val['earnings_growth'] + 0.3 * val['roic']
                      - 0.01 * val['ev_ebitda'] - 0.05 * val['debt_to_equity'], 4)
        passed.append({'ticker': s['ticker'], 'score': score, 'ev_ebitda': val['ev_ebitda'],
                       'roic': val['roic'], 'altman_z': val['altman_z']})
    passed.sort(key=lambda x: -x['score'])
    return passed


def score_stock_fundamentals_with_audit(stocks, max_debt_eq=2.5, max_ev_ebitda=20.0, min_roic=0.10, min_altman_z=1.8):
    """Same screen, but also returns why each name was rejected."""
    audit = []
    required = ('fcf_yield', 'earnings_growth', 'debt_to_equity', 'ev_ebitda', 'roic', 'altman_z')
    for s in stocks:
        ticker = s.get('ticker', '?')
        missing = [k for k in required if _num(s.get(k)) is None]
        if missing or not s.get('ticker'):
            audit.append({'ticker': ticker, 'reason': 'missing ' + ', '.join(missing or ['ticker'])})
            continue
        val = {k: _num(s[k]) for k in required}
        if val['debt_to_equity'] > max_debt_eq or val['fcf_yield'] <= 0:
            audit.append({'ticker': ticker, 'reason': 'leverage above limit or non-positive FCF yield'})
        elif val['ev_ebitda'] <= 0 or val['ev_ebitda'] > max_ev_ebitda:
            audit.append({'ticker': ticker, 'reason': 'EV/EBITDA outside range'})
        elif val['roic'] < min_roic or val['altman_z'] < min_altman_z:
            audit.append({'ticker': ticker, 'reason': 'ROIC or Altman Z below floor'})
        else:
            audit.append({'ticker': ticker, 'reason': 'passed'})
    kept = {r['ticker']: r for r in score_stock_fundamentals(stocks, max_debt_eq=max_debt_eq,
                                                              max_ev_ebitda=max_ev_ebitda,
                                                              min_roic=min_roic, min_altman_z=min_altman_z)}
    return {'passed': list(kept.values()),
            'rejected': [r for r in audit if r['reason'] != 'passed'],
            'audit': audit}


def macro_micro_monte_carlo(base_return, base_vol, growth_z=0.0, rate_shock=0.0, trials=500, seed=42):
    """Simulate macro shock impact on a firm's return distribution and compute Fractional Kelly size."""
    mu0, vol = _num(base_return), _num(base_vol)
    if mu0 is None or vol is None or vol <= 0:
        return {'expected_return': None, 'prob_positive': None, 'kelly_size': 0.0, 'reason': 'missing or invalid inputs'}
    gz = _num(growth_z) or 0.0
    rs = _num(rate_shock) or 0.0
    trials = max(2, int(trials))
    rng = random.Random(seed)
    adj_mean = mu0 + 0.03 * gz - 0.04 * rs
    sims = [rng.gauss(adj_mean, vol) for _ in range(trials)]
    var = statistics.variance(sims) if len(sims) >= 2 else vol ** 2
    mean = statistics.mean(sims)
    kelly = kelly_fraction(mean, var, fraction=0.5, max_weight=0.40)
    prob_pos = sum(1 for r in sims if r > 0) / trials
    return {'expected_return': round(mean, 4), 'prob_positive': round(prob_pos, 4), 'kelly_size': kelly}


def score_sector_relative(stocks, min_interest_cov=3.0):
    """Filter by interest coverage (>=3.0x) and rank stocks relative to their own sector's average EV/EBITDA."""
    valid = []
    for s in stocks:
        cov = _num(s.get('interest_coverage'))
        ev = _num(s.get('ev_ebitda'))
        if cov is None:
            continue  # fail closed: unknown coverage cannot be assumed above the floor
        if cov < min_interest_cov or ev is None or ev <= 0:
            continue
        valid.append({'ticker': s.get('ticker', '?'), 'sector': s.get('sector', 'ALL'), 'ev_ebitda': ev})
    by_sec = {}
    for s in valid:
        by_sec.setdefault(s['sector'], []).append(s['ev_ebitda'])
    sec_avg = {sec: statistics.mean(vals) for sec, vals in by_sec.items()}
    out = []
    for s in valid:
        avg_ev = sec_avg[s['sector']]
        # A non-positive sector average cannot be divided by. The old code silently scored the
        # name 0.0, which put an unmeasurable stock in the middle of the ranking — better than a
        # cheap stock and worse than an expensive one, on no evidence. It is now dropped and said
        # so, so the caller can see the name was not ranked rather than ranked as average.
        if avg_ev <= 0:
            continue
        rel_val = round((avg_ev - s['ev_ebitda']) / avg_ev, 4)
        out.append({'ticker': s['ticker'], 'sector': s['sector'], 'sector_rel_value': rel_val})
    out.sort(key=lambda x: (-x['sector_rel_value'], x['ticker']))  # deterministic tie-break
    return out


def risk_parity_allocation(assets, safety_tilt=1.5, max_weight=0.45):
    """Standard (1/vol), Safer-Tilted (1/vol^tilt) and Return-Adjusted risk parity.

    Caps hold after normalisation; anything that cannot be placed is reported as ``cash``.
    """
    empty = {'standard': {}, 'adjusted_safe': {}, 'return_adjusted': {}, 'cash': 1.0, 'safety_tilt': safety_tilt}
    if not assets:
        return empty
    vols, rets = {}, {}
    for a in assets:
        sym = str(a.get('symbol', '') or '').upper()
        vol = _num(a.get('volatility', a.get('vol')))
        ret = _num(a.get('expected_return', a.get('return')))
        if not sym or vol is None or vol <= 0:
            continue
        vols[sym] = max(vol, 0.01)
        rets[sym] = max(ret or 0.0, 0.0)
    if not vols or not _num(max_weight) or float(max_weight) <= 0:
        return empty
    max_weight = min(float(max_weight), 1.0)
    caps = {k: max_weight for k in vols}

    std_raw = {k: 1.0 / v for k, v in vols.items()}
    s = sum(std_raw.values())
    standard, std_cash = _water_fill({k: v / s for k, v in std_raw.items()}, caps)

    t = _num(safety_tilt) or 1.5
    adj_raw = {k: 1.0 / (v ** t) for k, v in vols.items()}
    a_sum = sum(adj_raw.values())
    adjusted_safe, safe_cash = _water_fill({k: v / a_sum for k, v in adj_raw.items()}, caps)

    ret_raw = {k: (1.0 + rets[k]) / (vols[k] ** 1.25) for k in vols}
    r_sum = sum(ret_raw.values())
    return_adjusted, ret_cash = _water_fill({k: v / r_sum for k, v in ret_raw.items()}, caps)

    return {
        'standard': {k: round(v, 4) for k, v in standard.items()},
        'adjusted_safe': {k: round(v, 4) for k, v in adjusted_safe.items()},
        'return_adjusted': {k: round(v, 4) for k, v in return_adjusted.items()},
        'cash': {'standard': round(std_cash, 4), 'adjusted_safe': round(safe_cash, 4),
                 'return_adjusted': round(ret_cash, 4)},
        'safety_tilt': t,
    }


def kalman_filter_series(series, process_var=1e-4, meas_var=1e-2):
    """1D Kalman Filter for smoothing noisy macro (inflation, rates, GDP) and micro (earnings, margins) signals."""
    clean = [v for v in (_num(x) for x in (series or [])) if v is not None]
    if not clean:
        return {'filtered': [], 'latest': None, 'uncertainty': None}
    x = float(clean[0])
    p_cov = 1.0
    filtered = []
    for z in clean:
        p_pred = p_cov + process_var
        k_gain = p_pred / (p_pred + meas_var)
        x = x + k_gain * (z - x)
        p_cov = (1.0 - k_gain) * p_pred
        filtered.append(round(x, 6))
    return {'filtered': filtered, 'latest': filtered[-1], 'uncertainty': round(p_cov, 6)}


def correlation_matrix(series_map, min_obs=4):
    """Pearson correlation on the overlapping tail of each pair.

    Pairs with fewer than ``min_obs`` common observations, or with no variation, are omitted rather
    than reported as 0.0 (which would look like a genuine "uncorrelated" reading).
    """
    clean = {}
    for k, v in (series_map or {}).items():
        vals = [x for x in (_num(z) for z in v) if x is not None]
        if len(vals) >= min_obs:
            clean[k] = vals
    keys = list(clean)
    mat = {k: {} for k in keys}
    for i, ki in enumerate(keys):
        for kj in keys[i:]:
            n = min(len(clean[ki]), len(clean[kj]))
            if n < min_obs:
                continue
            a, b = clean[ki][-n:], clean[kj][-n:]
            ma, mb = sum(a) / n, sum(b) / n
            num = sum((va - ma) * (vb - mb) for va, vb in zip(a, b))
            da = math.sqrt(sum((va - ma) ** 2 for va in a))
            db = math.sqrt(sum((vb - mb) ** 2 for vb in b))
            pair = round(num / (da * db), 4) if da > 0 and db > 0 else None
            mat[ki][kj] = 1.0 if ki == kj else pair
            if ki != kj and pair is not None:
                mat[kj][ki] = pair
    return mat


def markov_regime_chain(regimes, steps=1):
    """Estimate Markov transition matrix across market valuation regimes and project next-step probabilities."""
    if len(regimes) < 2:
        cur = regimes[-1] if regimes else "NEUTRAL"
        return {"current_regime": cur, "transition_matrix": {}, "next_probabilities": {cur: 1.0}}
    states = sorted(set(regimes))
    counts = {s: {t: 0 for t in states} for s in states}
    for a, b in zip(regimes[:-1], regimes[1:]):
        counts[a][b] += 1
    matrix = {}
    for s in states:
        tot = sum(counts[s].values())
        matrix[s] = {t: round(counts[s][t] / tot, 4) if tot > 0 else round(1.0 / len(states), 4) for t in states}
    dist = {s: (1.0 if s == regimes[-1] else 0.0) for s in states}
    for _ in range(max(1, int(steps))):
        nxt = {t: 0.0 for t in states}
        for s, p_s in dist.items():
            for t, p_st in matrix[s].items():
                nxt[t] += p_s * p_st
        dist = nxt
    return {
        "current_regime": regimes[-1],
        "transition_matrix": matrix,
        "next_probabilities": {k: round(v, 4) for k, v in dist.items()},
    }


def black_swan_heat_diffusion(assets, shock_temp=2.5, diffusion=0.35, seed=42, confidence=0.99, horizon_days=21):
    """Parametric tail-risk stress for a portfolio, plus an explicit diffusion step.

    ``tail_var_99`` is a genuine quantile over a stated ``horizon_days`` horizon:
    ``z(confidence) * vol * sqrt(h/252) * shock_temp``. It is deterministic for a given input, so the
    recommended cash buffer no longer swings between 10% and 60% because the random seed changed.

    ``shock_temp`` is an explicit stress multiplier (2.5 = a 2.5-sigma-of-sigma event), and
    ``diffusion`` blends each asset's tail toward the average, standing in for contagion. Neither is
    fitted to data; they are knobs, and the horizon is reported back so a user can judge them.
    """
    clean = {}
    for a in assets or []:
        sym = str(a.get('symbol', '') or '').upper()
        vol = _num(a.get('volatility', a.get('vol')))
        if sym and vol is not None and vol > 0:
            clean[sym] = vol
    if not clean:
        return {"stressed_assets": {}, "tail_var_99": 0.0, "recommended_cash_buffer": 0.25,
                "cash_buffer": 0.25, "horizon_days": horizon_days, "confidence": confidence}
    z = statistics.NormalDist().inv_cdf(min(max(float(confidence), 1e-6), 1 - 1e-9))
    temp = _num(shock_temp) or 2.5
    h = max(1, int(_num(horizon_days) or 21))
    scale = math.sqrt(h / 252.0)
    raw = {k: z * v * scale * temp for k, v in clean.items()}
    spread = _num(diffusion) or 0.0
    spread = max(0.0, min(1.0, spread))
    avg = sum(raw.values()) / len(raw)
    stressed = {k: round((1.0 - spread) * v + spread * avg, 4) for k, v in raw.items()}
    tail = round(max(stressed.values()), 4)
    buffer = round(min(0.60, max(0.10, tail * 0.5)), 4)
    return {"stressed_assets": stressed, "tail_var_99": tail,
            "recommended_cash_buffer": buffer, "cash_buffer": buffer,
            "horizon_days": h, "confidence": confidence, "stress_multiplier": temp}


def walk_forward_backtest(returns, risk_free=0.02, fee_bps=5.0, periods_per_year=252, turnovers=None):
    """Performance statistics for a return stream: cumulative return, annualised Sharpe, max DD.

    Despite the name this does not walk anything forward: it scores returns you supply. For a
    point-in-time replay of the actual strategy use ``backtest.replay``.

    **Fees.** With ``turnovers`` (one two-sided turnover per period, e.g. 0.20 for a 10% switch)
    the fee is ``fee_bps * turnover`` — costs follow trading, as they do in reality. Without it the
    legacy flat charge of ``fee_bps`` every period is applied and ``fee_basis`` says so; that
    over-charges a low-turnover strategy and under-charges a high-turnover one.
    """
    clean = [v for v in (_num(r) for r in (returns or [])) if v is not None]
    if not clean:
        return {"cumulative_return": 0.0, "sharpe": 0.0, "max_drawdown": 0.0, "periods": 0}
    fee = float(fee_bps or 0.0) / 10000.0
    ppy = max(1, int(periods_per_year or 252))
    tos = None
    if turnovers is not None:
        tos = [(_num(t) or 0.0) for t in turnovers]
        tos = (tos + [0.0] * len(clean))[:len(clean)]
    equity, peak, max_dd = 1.0, 1.0, 0.0
    net, total_fee = [], 0.0
    for i, r in enumerate(clean):
        charge = fee * max(0.0, tos[i]) if tos is not None else fee
        total_fee += charge
        step = float(r) - charge
        net.append(step)
        equity *= (1.0 + step)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    mean_r = sum(net) / len(net)
    var_r = sum((r - mean_r) ** 2 for r in net) / max(len(net) - 1, 1)
    std_r = var_r ** 0.5
    rf_per = float(risk_free or 0.0) / ppy
    undefined = std_r <= 1e-12
    sharpe = ((mean_r - rf_per) / std_r) * math.sqrt(ppy) if not undefined else None
    return {"cumulative_return": round(equity - 1.0, 4),
            "sharpe": round(sharpe, 3) if sharpe is not None else None,
            "sharpe_undefined": undefined,
            "max_drawdown": round(max_dd, 4), "periods": len(net),
            "fees_paid": round(total_fee, 6),
            "fee_basis": 'turnover' if tos is not None else 'flat_per_period'}


def format_jarvis_alert(regime, cash_buffer, tail_var):
    return f"[OBSIDIAN ECONOMICS // J.A.R.V.I.S.] Regime={regime} | CashBuffer={cash_buffer*100:.1f}% | TailVaR99={tail_var:.2f}"


def drawdown_circuit_breaker(equity_curve, max_dd_limit=0.08):
    """Hard circuit breaker: if drawdown >= max_dd_limit (default 8%), force >= 50% defensive cash.

    Non-positive or non-finite NAV points are treated as unusable and trigger the breaker rather than
    being ignored.
    """
    curve = [v for v in (_num(x) for x in (equity_curve or [])) if v is not None]
    if not curve or any(v <= 0 for v in curve):
        return {"triggered": bool(curve), "current_drawdown": 1.0 if curve else 0.0,
                "forced_cash_floor": 0.50 if curve else 0.10, "reason": "invalid equity curve"}
    peak = max(curve)
    cur = curve[-1]
    dd = (peak - cur) / peak if peak > 0 else 0.0
    triggered = dd >= max_dd_limit
    return {"triggered": triggered, "current_drawdown": round(dd, 4),
            "forced_cash_floor": 0.50 if triggered else 0.10}


def stage_broker_order(symbol, side, qty, approved=False):
    """Human-in-the-loop order staging: never executes live without explicit approved=True."""
    q = _num(qty)
    if q is None or q <= 0:
        raise ValueError('order quantity must be a positive number')
    return {
        "symbol": str(symbol).upper(),
        "side": str(side).upper(),
        "qty": round(q, 4),
        "status": "READY_TO_EXECUTE" if approved else "STAGED_AWAITING_APPROVAL",
        "approved": bool(approved),
    }


def fx_hedge_ratio(asset_vol, fx_vol, corr=0.0):
    """Minimum-variance FX hedge ratio (fraction of the currency exposure to hedge).

    For an unhedged local-currency asset plus FX, variance is
    ``sa^2 + (1-h)^2 sf^2 + 2(1-h) rho sa sf``. Minimising gives ``h = 1 + rho*sa/sf``:
    full hedge when uncorrelated, above 1 when the asset is positively FX-correlated, below 1 when
    negatively correlated. Clamped to [-1, 2] to keep the recommendation interpretable.
    """
    sa, sf = _num(asset_vol), _num(fx_vol)
    if sa is None or sa < 0 or sf is None or sf <= 0:
        return None
    rho = _num(corr) or 0.0
    rho = max(-1.0, min(1.0, rho))
    h = 1.0 + rho * sa / sf
    return round(max(-1.0, min(2.0, h)), 4)


def backup_state_dir(src_dir, backup_dir):
    """Create a timestamped zip backup of MarketLens state and J.A.R.V.I.S. reports."""
    import datetime, zipfile
    from pathlib import Path
    src, dst = Path(src_dir), Path(backup_dir)
    dst.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = dst / f"MarketLens_Backup_{ts}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if src.exists():
            for p in src.rglob("*"):
                if p.is_file():
                    z.write(p, p.relative_to(src))
    return str(out)


def diversification_score(corr_matrix):
    """Ray Dalio check: average off-diagonal correlation across genuinely comparable pairs."""
    keys = [k for k in (corr_matrix or {}) if isinstance(corr_matrix[k], dict)]
    vals = [corr_matrix[a][b] for i, a in enumerate(keys) for b in keys[i + 1:]
            if isinstance(corr_matrix[a].get(b), (int, float))]
    if not vals:
        return {"avg_correlation": None, "diversified": None, "pairs": 0}
    avg_c = sum(vals) / len(vals)
    return {"avg_correlation": round(avg_c, 4), "diversified": avg_c < 0.45, "pairs": len(vals)}


def statistical_edge_zscore(returns):
    """Jim Simons check: test whether mean return is statistically significant (t-stat >= 2.0) vs. noise."""
    clean = [v for v in (_num(r) for r in (returns or [])) if v is not None]
    n = len(clean)
    if n < 2:
        return {"t_stat": 0.0, "statistically_significant": False}
    mean_r = sum(clean) / n
    var_r = sum((r - mean_r) ** 2 for r in clean) / (n - 1)
    se = (var_r / n) ** 0.5
    t_stat = mean_r / se if se > 1e-12 else 0.0
    return {"t_stat": round(t_stat, 3), "statistically_significant": t_stat >= 2.0}


def dalio_quadrant_weights(growth_trend, inflation_trend):
    """Ray Dalio All-Weather 4-quadrant macro allocation.

    Derived from ``asset_classes.ALL_WEATHER_MAP`` so it can never disagree with the allocation
    the strategy actually reports (there used to be two different tables). Buckets map to the
    coarse names this function has always returned: TREASURY -> bonds, GOLD + COMMODITY ->
    commodities, TBILL -> cash.
    """
    from asset_classes import ALL_WEATHER_MAP
    quad = classify_macro_quadrant(growth_trend, inflation_trend)
    table = ALL_WEATHER_MAP.get(quad, ALL_WEATHER_MAP['unknown'])
    w = {"equities": table.get('EQUITY', 0.0),
         "bonds": table.get('TREASURY', 0.0),
         "commodities": round(table.get('GOLD', 0.0) + table.get('COMMODITY', 0.0), 6),
         "cash": table.get('TBILL', 0.0)}
    return {"quadrant": quad.upper(), "weights": w}


def half_kelly_position_size(expected_return, volatility, max_cap=0.35, risk_free=0.0):
    """Jim Simons Half-Kelly sizing: f* = 0.5 * (mu - rf) / sigma^2, capped at max_cap.

    The risk-free rate is subtracted, so "edge" means excess return over cash. Use
    ``alpha_engine.kelly_detail`` when you need to see whether the cap or the edge set the size.
    """
    return kelly_detail(expected_return, volatility, fraction=0.5, max_weight=max_cap,
                        risk_free=risk_free)['capped']


def bear_market_alpha(regime, rsi, price_to_intrinsic, cash_buffer):
    """Buffett (deep-value panic deployment) + Kotegawa/BNF (oversold capitulation bounce) + Bear-market hedge."""
    actions = []
    rsi_v = _num(rsi)
    rel = _num(price_to_intrinsic)
    cash = _num(cash_buffer) or 0.0
    if str(regime).upper() == "BEAR":
        actions.append("HEDGE_BEAR_REGIME (TLT/GLD/Inverse)")
    if rel is not None and 0 < rel <= 0.75 and cash >= 0.15:
        actions.append("BUFFETT_PANIC_DEPLOY (25%+ discount to intrinsic value)")
    if rsi_v is not None and rsi_v <= 25.0:
        actions.append("KOTEGAWA_OVERSOLD_BOUNCE (RSI <= 25 mean-reversion)")
    if not actions:
        actions.append("HOLD_DEFENSIVE_CASH")
    return {"regime": regime, "rsi": rsi_v, "price_to_intrinsic": rel, "actions": actions}


def vol_target_scale(current_vol, target_vol=0.10, max_scale=1.25, min_scale=0.50):
    """Scale exposure up in calm markets and down in panics to keep portfolio risk constant."""
    v = _num(current_vol)
    t = _num(target_vol)
    if v is None or v <= 0 or t is None or t <= 0:
        return min_scale
    return round(max(min_scale, min(max_scale, t / v)), 4)


def trailing_stop_lock(entry_price, peak_price, current_price, trail_pct=0.06, min_gain=0.15):
    """Lock in profits once a position gains >= min_gain (15%) by trailing a stop at trail_pct (6%) below peak."""
    e, p, c = _num(entry_price), _num(peak_price), _num(current_price)
    if e is None or p is None or c is None or e <= 0:
        return {"active": False, "stop_price": None, "exit_triggered": False, "reason": "invalid prices"}
    peak = max(p, e)
    gain = (peak - e) / e
    stop_price = peak * (1.0 - float(trail_pct))
    active = gain >= min_gain
    return {"active": active, "stop_price": round(stop_price, 4), "exit_triggered": bool(active and c <= stop_price),
            "unrealized_gain": round((c - e) / e, 4)}


def rebalance_with_drift_threshold(current_weights, target_weights, min_drift=0.03):
    """Only trade assets whose weight drifted by >= min_drift; positions absent from the target are
    sold down to zero explicitly, and any target residual is reported as cash."""
    trades = {}
    target_weights = {k: float(v) for k, v in (target_weights or {}).items() if _num(v) is not None}
    current_weights = {k: float(v) for k, v in (current_weights or {}).items() if _num(v) is not None}
    for sym in set(current_weights) | set(target_weights):
        cur = current_weights.get(sym, 0.0)
        tgt = target_weights.get(sym, 0.0)
        diff = tgt - cur
        if abs(diff) >= min_drift or (cur > 0 and tgt == 0.0):
            trades[sym] = round(diff, 4)
    invested = sum(max(0.0, v) for v in target_weights.values())
    return {"rebalance_needed": bool(trades), "trades": trades,
            "cash": round(max(0.0, 1.0 - invested), 4)}


def apply_sector_cap(weights, sector_map, max_sector_weight=0.30):
    """Cap total allocation per sector at max_sector_weight, then split each sector across its members.

    Caps are applied to the *sector total* first. If the sectors cannot together hold 100% under
    that cap, the unplaceable remainder is left as cash instead of being pushed back into a capped
    sector (the previous code renormalised after capping, which always undid the cap).
    """
    if not weights:
        return {}
    mw = _num(max_sector_weight)
    if mw is None or mw <= 0:
        raise ValueError('max_sector_weight must be positive')
    mw = min(float(mw), 1.0)
    members = {}
    for sym, w in weights.items():
        members.setdefault(sector_map.get(sym, "OTHER"), []).append(sym)
    totals = {sec: sum(max(0.0, float(weights[s])) for s in syms) for sec, syms in members.items()}
    grand = sum(totals.values())
    if grand <= 0:
        return {}
    norm = {sec: t / grand for sec, t in totals.items()}
    sector_caps = {sec: mw for sec in totals}
    capped, _cash = _water_fill(norm, sector_caps)
    out = {}
    for sec, ins in members.items():
        allowed = capped.get(sec, 0.0)
        base = totals[sec]
        for sym in ins:
            share = (max(0.0, float(weights[sym])) / base) if base > 0 else 0.0
            out[sym] = round(allowed * share, 4)
    return out


def apply_sector_cap_report(weights, sector_map, max_sector_weight=0.30):
    """``apply_sector_cap`` plus the unallocated cash it had to leave to honour the cap."""
    capped = apply_sector_cap(weights, sector_map, max_sector_weight)
    return {'weights': capped, 'cash': round(max(0.0, 1.0 - sum(capped.values())), 4)}


def liquidity_and_earnings_gate(avg_daily_dollar_vol, days_to_earnings=None, min_dollar_vol=10_000_000.0, blackout_days=3):
    """Block illiquid stocks (< $10M/day) and binary pre-earnings gambles (<= 3 days to earnings)."""
    adv = _num(avg_daily_dollar_vol)
    liquid = adv is not None and adv >= min_dollar_vol
    dte = _num(days_to_earnings)
    earnings_known = dte is not None
    earnings_safe = True if not earnings_known else dte > blackout_days
    return {"allowed": liquid and earnings_safe, "liquid": liquid, "earnings_safe": earnings_safe,
            "earnings_unknown": not earnings_known}


def out_of_sample_split_test(returns, train_ratio=0.70, fee_bps=5.0):
    """Anti-overfitting check: compare in-sample (70%) vs. unseen out-of-sample (30%) Sharpe."""
    clean = [v for v in (_num(r) for r in (returns or [])) if v is not None]
    if len(clean) < 10:
        return {"in_sample_sharpe": 0.0, "out_of_sample_sharpe": 0.0, "overfit_warning": False, "reason": "too few observations"}
    split = min(len(clean) - 2, max(2, int(len(clean) * train_ratio)))
    in_s = walk_forward_backtest(clean[:split], fee_bps=fee_bps)["sharpe"]
    out_s = walk_forward_backtest(clean[split:], fee_bps=fee_bps)["sharpe"]
    if in_s is None or out_s is None:
        return {"in_sample_sharpe": in_s, "out_of_sample_sharpe": out_s,
                "overfit_warning": False, "reason": "one segment has zero variance"}
    overfit = (in_s > 0.5) and (out_s < 0.5 * in_s)
    return {"in_sample_sharpe": in_s, "out_of_sample_sharpe": out_s, "overfit_warning": overfit}


def tax_aware_exit_check(holding_days, unrealized_pnl_pct):
    """Favor long-term capital gains (>= 365 days) on winners; allow tax-loss harvesting on <= -5% losers."""
    d = _num(holding_days)
    pnl = _num(unrealized_pnl_pct)
    if d is None or pnl is None:
        return {"long_term_gains": False, "harvest_loss": False, "hold_for_tax": False, "reason": "invalid input"}
    is_ltcg = d >= 365
    harvest_loss = pnl <= -0.05
    hold_for_tax = pnl > 0.0 and not is_ltcg
    return {"long_term_gains": is_ltcg, "harvest_loss": harvest_loss, "hold_for_tax": hold_for_tax}


def fast_shock_detector(latest_return, daily_vol):
    """Instant 3-sigma shock detector so the engine reacts immediately without waiting for Kalman/Markov lag."""
    r, v = _num(latest_return), _num(daily_vol)
    if r is None or v is None or v <= 0:
        return {"z_shock": None, "shock_triggered": False, "reason": "invalid input"}
    z = abs(r) / max(v, 0.005)
    return {"z_shock": round(z, 2), "shock_triggered": z >= 3.0}


def detect_1h_order_block_and_candle(candles_1h):
    """Detect 1H institutional Order Block (bullish/bearish) and candlestick confirmation (Engulfing / Pinbar)."""
    if not candles_1h or len(candles_1h) < 3:
        return {"order_block": None, "candle_confirmation": None, "signal": "NONE"}
    try:
        c_prev, c_curr = candles_1h[-2], candles_1h[-1]
        o1, h1, l1, cl1 = (float(c_prev[k]) for k in ("open", "high", "low", "close"))
        o2, h2, l2, cl2 = (float(c_curr[k]) for k in ("open", "high", "low", "close"))
    except (KeyError, TypeError, ValueError):
        return {"order_block": None, "candle_confirmation": None, "signal": "NONE", "reason": "malformed candle"}
    if min(o1, h1, l1, cl1, o2, h2, l2, cl2) < 0 or h1 < l1 or h2 < l2:
        return {"order_block": None, "candle_confirmation": None, "signal": "NONE", "reason": "impossible OHLC"}

    ob = None
    if cl1 < o1 and cl2 > h1:
        ob = {"type": "BULLISH_OB", "zone_low": l1, "zone_high": h1}
    elif cl1 > o1 and cl2 < l1:
        ob = {"type": "BEARISH_OB", "zone_low": l1, "zone_high": h1}

    body2 = abs(cl2 - o2)
    rng2 = max(h2 - l2, 1e-6)
    lower_wick = min(o2, cl2) - l2
    upper_wick = h2 - max(o2, cl2)

    conf = None
    if cl2 > o2 and cl1 < o1 and cl2 >= o1 and o2 <= cl1:
        conf = "BULLISH_ENGULFING"
    elif cl2 < o2 and cl1 > o1 and cl2 <= o1 and o2 >= cl1:
        conf = "BEARISH_ENGULFING"
    elif body2 > 0 and lower_wick >= 2.0 * body2 and (lower_wick / rng2) >= 0.55:
        conf = "BULLISH_PINBAR"
    elif body2 > 0 and upper_wick >= 2.0 * body2 and (upper_wick / rng2) >= 0.55:
        conf = "BEARISH_PINBAR"

    signal = "LONG" if (ob and ob["type"] == "BULLISH_OB" and conf in ("BULLISH_ENGULFING", "BULLISH_PINBAR")) else (
        "SHORT" if (ob and ob["type"] == "BEARISH_OB" and conf in ("BEARISH_ENGULFING", "BEARISH_PINBAR")) else "WATCH" if ob else "NONE"
    )
    return {"order_block": ob, "candle_confirmation": conf, "signal": signal}
