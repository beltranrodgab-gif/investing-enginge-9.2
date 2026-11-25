"""Spread an account across a ladder of operations with different risk and time horizons.

One account is not one bet. This module splits it into *operations*: sleeves that differ in how
much risk they take and how long the money can stay at work — overnight cash, one-month
liquidity, one-year income, multi-year core, growth, real assets and a small tactical sleeve.

Three stages, each doing one job:

1. every operation gets the best portfolio *for its own risk budget*, from
   ``efficient_frontier.portfolio_at_vol``;
2. the account's efficient frontier is then computed **across the operations**, not across the
   ETFs, so each frontier point is a genuine spread of money over several time horizons;
3. fast Monte Carlo scores every frontier point on the probability of reaching the account goal
   inside a drawdown limit, and the best point is chosen.

Per-operation floors are structural, not a penalty: if ``f`` is the floor vector and
``s = 1 - sum(f)``, then ``w = f + s*y`` always respects the floors, caps map to caps on ``y``,
and the frontier solve runs unchanged in ``y`` space. The output therefore cannot collapse into
a single sleeve.

Expected returns are explicit long-run priors. They are inputs, not forecasts, and the plan says
so in its ``basis`` field.
"""
import math

import efficient_frontier as ef


# --- Priors ---------------------------------------------------------------------------------
# name: (asset_class, expected annual return, annual volatility). Ordering is deliberate: the
# ladder's expected return should not fall as its risk budget rises, or the "ladder" is a lie.
DEFAULT_ASSETS = {
    'BIL':  ('cash',        0.040, 0.003),
    'SHY':  ('short_bond',  0.043, 0.015),
    'IEF':  ('bond',        0.046, 0.055),
    'LQD':  ('credit',      0.052, 0.070),
    'TLT':  ('long_bond',   0.052, 0.130),
    'SPY':  ('equity',      0.075, 0.160),
    'VEA':  ('intl_equity', 0.070, 0.180),
    'QQQ':  ('growth',      0.090, 0.225),
    'GLD':  ('gold',        0.048, 0.150),
    'DBC':  ('commodity',   0.050, 0.200),
}

# Cross-asset-class correlation priors. Symmetric; unlisted pairs fall back to 0.25.
CLASS_CORR = {
    ('cash', 'short_bond'): 0.30, ('cash', 'bond'): 0.10, ('cash', 'long_bond'): 0.05,
    ('cash', 'credit'): 0.10, ('cash', 'equity'): 0.00, ('cash', 'growth'): 0.00,
    ('cash', 'intl_equity'): 0.00, ('cash', 'gold'): 0.00, ('cash', 'commodity'): 0.00,
    ('short_bond', 'bond'): 0.75, ('short_bond', 'long_bond'): 0.60,
    ('short_bond', 'credit'): 0.55, ('short_bond', 'equity'): 0.05,
    ('short_bond', 'growth'): 0.02, ('short_bond', 'intl_equity'): 0.05,
    ('short_bond', 'gold'): 0.10, ('short_bond', 'commodity'): 0.00,
    ('bond', 'long_bond'): 0.85, ('bond', 'credit'): 0.70, ('bond', 'equity'): -0.10,
    ('bond', 'growth'): -0.15, ('bond', 'intl_equity'): -0.05, ('bond', 'gold'): 0.15,
    ('bond', 'commodity'): -0.05,
    ('long_bond', 'credit'): 0.65, ('long_bond', 'equity'): -0.20,
    ('long_bond', 'growth'): -0.25, ('long_bond', 'intl_equity'): -0.10,
    ('long_bond', 'gold'): 0.20, ('long_bond', 'commodity'): -0.05,
    ('credit', 'equity'): 0.45, ('credit', 'growth'): 0.40, ('credit', 'intl_equity'): 0.40,
    ('credit', 'gold'): 0.05, ('credit', 'commodity'): 0.10,
    ('equity', 'growth'): 0.88, ('equity', 'intl_equity'): 0.82, ('equity', 'gold'): 0.05,
    ('equity', 'commodity'): 0.25,
    ('growth', 'intl_equity'): 0.72, ('growth', 'gold'): 0.00, ('growth', 'commodity'): 0.20,
    ('intl_equity', 'gold'): 0.10, ('intl_equity', 'commodity'): 0.30,
    ('gold', 'commodity'): 0.35,
}


def class_corr(a, b, default=0.25):
    if a == b:
        return 1.0
    return CLASS_CORR.get((a, b), CLASS_CORR.get((b, a), default))


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


# --- The ladder -----------------------------------------------------------------------------
# ``floor``/``cap`` bind at the *account* level, so they express "keep at least this much
# overnight" and "never put more than this into the tactical sleeve".
OPERATIONS = (
    {'id': 'overnight_cash', 'label': 'Overnight cash', 'horizon': '1 day', 'horizon_days': 1,
     'risk_tier': 1, 'target_vol': 0.004, 'floor': 0.05, 'cap': 0.40, 'universe': ['BIL']},
    {'id': 'one_month', 'label': 'One-month liquidity', 'horizon': '1 month', 'horizon_days': 30,
     'risk_tier': 1, 'target_vol': 0.020, 'floor': 0.05, 'cap': 0.30, 'universe': ['SHY']},
    {'id': 'one_year_income', 'label': 'One-year income', 'horizon': '1 year', 'horizon_days': 365,
     'risk_tier': 2, 'target_vol': 0.055, 'floor': 0.05, 'cap': 0.30, 'universe': ['IEF', 'LQD']},
    {'id': 'long_duration', 'label': 'Long duration', 'horizon': '2-5 years', 'horizon_days': 1095,
     'risk_tier': 3, 'target_vol': 0.120, 'floor': 0.05, 'cap': 0.25, 'universe': ['TLT', 'IEF']},
    {'id': 'core_equity', 'label': 'Core equity', 'horizon': '3-5 years', 'horizon_days': 1460,
     'risk_tier': 4, 'target_vol': 0.160, 'floor': 0.15, 'cap': 0.55, 'universe': ['SPY', 'VEA']},
    {'id': 'growth_sleeve', 'label': 'Growth sleeve', 'horizon': '5+ years', 'horizon_days': 1825,
     'risk_tier': 5, 'target_vol': 0.225, 'floor': 0.05, 'cap': 0.25, 'universe': ['QQQ']},
    {'id': 'real_assets', 'label': 'Real assets', 'horizon': '6-18 months', 'horizon_days': 540,
     'risk_tier': 4, 'target_vol': 0.150, 'floor': 0.05, 'cap': 0.20,
     'universe': ['GLD', 'DBC']},
    # Short-horizon sleeve: low risk and diversified, so it is not a duplicate of growth. A
    # single-asset tactical arm would be an exact clone of another sleeve and make the account
    # covariance singular.
    {'id': 'tactical', 'label': 'Tactical (weeks)', 'horizon': '2-8 weeks', 'horizon_days': 45,
     'risk_tier': 3, 'target_vol': 0.100, 'floor': 0.02, 'cap': 0.12,
     'universe': ['SPY', 'GLD', 'IEF']},
)


def operation_inputs(operations=OPERATIONS, asset_vols=None, asset_mu=None, regime_scale=1.0,
                     rf=0.04):
    """Resolve each operation's sleeve: its frontier point, return, volatility and weights.

    The sleeve's own weights sum to one. Account-level limits are applied later, on the split
    *between* operations — applying a cap inside the sleeve only dilutes its volatility and
    makes the ladder's risk tiers meaningless.

    ``regime_scale`` (0..1) shrinks every risk budget together, so a defensive regime lowers the
    whole ladder without changing its shape. The floor of 0.05 keeps the solve well-posed.
    """
    vols, mus, classes = {}, {}, {}
    for name, (cls, mu, vol) in DEFAULT_ASSETS.items():
        user_vol = _num((asset_vols or {}).get(name))
        user_mu = _num((asset_mu or {}).get(name))
        vols[name] = user_vol if user_vol and user_vol > 0 else vol
        mus[name] = user_mu if user_mu is not None else mu
        classes[name] = cls
    scale = _num(regime_scale)
    scale = 1.0 if scale is None else max(0.0, min(1.0, scale))
    out = []
    for op in operations:
        universe = sorted(a for a in op['universe'] if a in vols)
        if not universe:
            continue
        names, cov = ef.covariance_from_vols(
            {a: vols[a] for a in universe},
            pairs={f'{a}|{b}': class_corr(classes[a], classes[b])
                   for a in universe for b in universe if a != b})
        mu = [mus[a] for a in names]
        budget = op['target_vol'] * max(scale, 0.05)
        point = ef.portfolio_at_vol(mu, cov, names, budget, points=64)
        if point is None:
            continue
        weights = {a: round(w, 9) for a, w in point['weights'].items() if w > 1e-9}
        if not weights:
            continue
        total = sum(weights.values())
        weights = {a: w / total for a, w in weights.items()}
        st = ef.portfolio_stats(weights, names, mu, cov)
        # A sleeve must be able to *meet* its risk budget, not only choose assets. A single-asset
        # sleeve (the growth arm is QQQ only) has one volatility, so no weighting inside it can
        # lower its risk. Holding part of the sleeve in cash is the honest way to respect a
        # smaller budget: the risky fraction shrinks, sleeve volatility becomes the budget, and
        # expected return shrinks with it. Without this a defensive regime changed nothing,
        # because the Monte Carlo simply moved money into the one sleeve it could not scale.
        #
        # ``weights`` stay normalised over the sleeve's assets and ``risky_fraction`` carries the
        # cash part, so covariance and dollar allocation remain correct downstream.
        risky = 1.0
        if st['volatility'] > budget > 0:
            risky = budget / st['volatility']
        # The part of the sleeve held back as cash is still money in the account. It earns the
        # risk-free rate, not nothing: charging it 0% charged a defensive plan for being defensive
        # and made a halved-risk sleeve look 0.9 of a point a year worse than it is.
        rate = _num(rf)
        rate = 0.0 if rate is None else rate
        sleeve_mu = risky * st['return'] + (1.0 - risky) * rate
        out.append({
            'id': op['id'], 'label': op['label'], 'horizon': op['horizon'],
            'horizon_days': op['horizon_days'], 'risk_tier': op['risk_tier'],
            'target_vol': op['target_vol'], 'floor': op['floor'], 'cap': op['cap'],
            'names': names, 'mu': sleeve_mu, 'volatility': st['volatility'] * risky,
            'risky_fraction': round(risky, 9), 'cash_mu': round(rate, 6),
            'weights': weights, 'classes': {a: classes[a] for a in names},
        })
    return out


def operation_covariance(ops, estimation_variance=1e-4):
    """Covariance between operations, built from their sleeves and the class correlations.

    A small ``estimation_variance`` (1% annual volatility, expressed as variance) is added to
    each diagonal. Priors are estimates, not measurements, and a duplicate sleeve would otherwise
    make the matrix exactly singular: the account solve must not fail just because two sleeves
    happen to hold the same assets.

    Each sleeve's ``volatility`` *already* includes its ``risky_fraction``, so the covariance is
    ``corr * vol_a * vol_b``. Multiplying by the risky fractions a second time understated every
    cross-sleeve correlation — a defensive plan with the risk halved showed 0.085 where the prior
    is 0.30 — and an understated correlation makes the book look better diversified than it is,
    which is the one direction this can be wrong in safely.
    """
    n = len(ops)
    m = [[0.0] * n for _ in range(n)]
    for i, a in enumerate(ops):
        for j, b in enumerate(ops):
            if i == j:
                m[i][j] = a['volatility'] ** 2 + estimation_variance
                continue
            cov = 0.0
            for x, wx in a['weights'].items():
                for y, wy in b['weights'].items():
                    cov += (wx * wy * a['volatility'] * b['volatility']
                            * class_corr(a['classes'][x], b['classes'][y]))
            m[i][j] = cov
    return m


def _floors_caps(ops, floors=None, caps=None):
    """Resolve floor and cap vectors, keyed by operation id, and check they are compatible."""
    fl, cp = [], []
    for op in ops:
        f = _num((floors or {}).get(op['id'], op.get('floor', 0.0))) or 0.0
        c = _num((caps or {}).get(op['id'], op.get('cap', 1.0)))
        c = 1.0 if c is None else c
        f = max(0.0, min(1.0, f))
        cp.append(max(f, min(1.0, c)))
        fl.append(f)
    return fl, cp


def operations_frontier(ops, op_cov, floors=None, caps=None, rf=0.04, points=32):
    """The account's efficient frontier, expressed as shares *between* operations.

    Floors are removed first: with ``f`` the floor vector and ``s = 1 - sum(f)``, a share vector
    is ``w = f + s*y``. Then ``w >= f`` holds by construction, ``w <= cap`` becomes
    ``y <= (cap - f)/s``, and minimising variance of ``w`` is the same problem as minimising
    variance of ``y`` scaled by ``s``. So the existing exact solver runs unchanged and cannot
    return a plan that collapses into one sleeve.

    Each point reports its own shares, return, volatility, Sharpe and KKT residual. Return is an
    allocation return over the whole account, so points are directly comparable.
    """
    ids = [o['id'] for o in ops]
    n = len(ops)
    if n == 0 or len(op_cov) != n:
        return {'names': ids, 'points': [], 'reason': 'no operations'}
    mu = [o['mu'] for o in ops]
    fl, cp = _floors_caps(ops, floors, caps)
    floor_total = sum(fl)
    if floor_total >= 1.0 - 1e-9:
        return {'names': ids, 'points': [], 'reason': 'floors already fill the account'}
    s = 1.0 - floor_total
    cap_y = [min(1.0, (cp[i] - fl[i]) / s) for i in range(n)]
    # Shares are w = f + s*y with sum(y) = 1. account return = base + s*(mu . y), and the
    # y-dependent variance is s^2 * y'Σy. A positive scale does not change the argmin, so the
    # solve runs on mu and Σ directly and only the *target* is converted.
    mu_y = list(mu)
    cov_y = [list(row) for row in op_cov]
    base = sum(fl[i] * mu[i] for i in range(n))

    def to_w(y):
        return [fl[i] + s * y[i] for i in range(n)]

    def stats(w, kkt=None):
        d = dict(zip(ids, w))
        st = ef.portfolio_stats(d, ids, mu, op_cov)
        return {'shares': d, 'return': st['return'], 'volatility': st['volatility'],
                'sharpe': (st['return'] - rf) / st['volatility'] if st['volatility'] > 0 else None,
                'kkt_residual': kkt}

    gmv = ef.min_variance_weights(mu_y, cov_y, None, cap_y)
    if not gmv['feasible']:
        return {'names': ids, 'points': [], 'reason': gmv['reason'] or 'infeasible floors/caps'}
    lo_pt = stats(to_w(gmv['weights']), gmv['kkt_residual'])
    y_hi = _greedy_max(mu_y, cap_y)
    hi_pt = stats(to_w(y_hi))
    # The greedy endpoint maximises return, it does not minimise variance, so a variance
    # optimality residual would be meaningless there. Say what the point is instead.
    hi_pt['method'] = 'max_return'
    lo, hi = lo_pt['return'], hi_pt['return']
    pts = [lo_pt]
    seen = {tuple(round(x, 6) for x in to_w(gmv['weights']))}
    # Interior targets only: a target equal to an endpoint makes the sub-problem degenerate, so
    # the endpoints are computed directly and never handed to the constrained solve.
    if hi - lo > 1e-9 and points > 2:
        span = hi - lo
        for i in range(1, points - 1):
            target_w = lo + span * i / (points - 1)
            # return_w = base + s*(mu . y), so the y-space target is (target_w - base)/s.
            target_y = (target_w - base) / s
            r = ef.min_variance_weights(mu_y, cov_y, target_y, cap_y)
            if not r['feasible']:
                continue
            w = to_w(r['weights'])
            key = tuple(round(x, 6) for x in w)
            if key in seen:
                continue
            seen.add(key)
            pts.append(stats(w, r['kkt_residual']))
    pts.append(hi_pt)
    pts.sort(key=lambda p: (p['volatility'], p['return']))
    tangency = max((p for p in pts if p['sharpe'] is not None),
                   key=lambda p: p['sharpe'], default=None)
    return {
        'names': ids,
        'rf': rf,
        'floors': dict(zip(ids, fl)),
        'caps': dict(zip(ids, cp)),
        'min_variance': lo_pt,
        'max_return': hi_pt,
        'tangency': tangency,
        'points': pts,
    }


def _greedy_max(mu, caps):
    """Highest-return weight vector under caps: fill by expected return."""
    w = [0.0] * len(mu)
    left = 1.0
    for i in sorted(range(len(mu)), key=lambda i: -mu[i]):
        if left <= 1e-12:
            break
        w[i] = max(0.0, min(caps[i], left))
        left -= w[i]
    return w


def choose_distribution(frontier, goal_multiple=1.25, years=3.0, max_drawdown=0.25,
                        paths=2500, seed=11):
    """Pick the frontier point most likely to reach the goal inside the drawdown limit.

    Scoring is the pathwise Monte Carlo, because a drawdown limit is a path question and the
    closed-form shortcut cannot answer it. Candidates are ordered deterministically, so an
    unchanged account and unchanged inputs always produce the same choice.
    """
    pts = [p for p in (frontier or {}).get('points', []) if p.get('volatility') is not None]
    if not pts:
        return {'chosen': None, 'candidates': [], 'reason': 'no frontier points'}
    rows = []
    for i, p in enumerate(pts):
        sim = ef.simulate_paths(p['return'], p['volatility'], years=years, paths=paths,
                                goal=goal_multiple, start=1.0, seed=seed + i)
        if not sim.get('ok'):
            continue
        dd = sim.get('max_drawdown', {})
        rows.append({
            'index': i, 'shares': p['shares'], 'return': p['return'],
            'volatility': p['volatility'], 'sharpe': p.get('sharpe'),
            'prob_hit_goal': sim.get('prob_hit_goal'),
            'median_wealth': sim.get('median_wealth'),
            'dd_p95': dd.get('p95'), 'dd_median': dd.get('median'),
        })
    if not rows:
        return {'chosen': None, 'candidates': [], 'reason': 'no usable frontier point'}
    inside = [r for r in rows if (r['dd_p95'] or 0.0) <= max_drawdown]
    warning = None
    pool = inside
    if not pool:
        pool = [min(rows, key=lambda r: (r['dd_p95'] or 0.0))]
        warning = 'no frontier point met the drawdown limit; returned the least risky'
    chosen = max(pool, key=lambda r: (r['prob_hit_goal'] or 0.0, r['median_wealth'] or 0.0,
                                     -(r['volatility'] or 0.0)))
    return {
        'chosen': chosen,
        'candidates': sorted(rows, key=lambda r: -(r['prob_hit_goal'] or 0.0)),
        'warning': warning,
        'evaluated': len(rows),
        'inside_drawdown_limit': len(inside),
        'goal_multiple': goal_multiple,
        'years': years,
        'max_drawdown': max_drawdown,
    }


def plan_account(ops, choice, account_value=10000.0, rf=0.04, regime='uncertain'):
    """Turn the chosen share vector into per-operation and per-asset money amounts.

    ``account_value`` is the only place money enters; every stage above works in weights. Both
    are reported so a caller can show dollars and check that they add up.
    """
    chosen = (choice or {}).get('chosen')
    if not chosen or not ops:
        return {'operations': [], 'assets': {}, 'invested': 0.0, 'cash': float(account_value or 0),
                'reason': 'no chosen distribution'}
    value = _num(account_value)
    if value is None or value < 0:
        return {'operations': [], 'assets': {}, 'invested': 0.0, 'cash': 0.0,
                'reason': 'account value must be non-negative'}
    shares = chosen['shares']
    rows, assets = [], {}
    for op in ops:
        share = _num(shares.get(op['id'])) or 0.0
        rows.append({
            'operation': op['id'], 'label': op['label'], 'horizon': op['horizon'],
            'horizon_days': op['horizon_days'], 'risk_tier': op['risk_tier'],
            'share': round(share, 6), 'amount': round(value * share, 2),
            'return': round(op['mu'], 6), 'volatility': round(op['volatility'], 6),
            'weights': {a: round(w, 6) for a, w in op['weights'].items()},
        })
        risky = op.get('risky_fraction', 1.0)
        for sym, w in op['weights'].items():
            assets[sym] = assets.get(sym, 0.0) + share * risky * w
    total_share = sum(_num(shares.get(o['id'])) or 0.0 for o in ops)
    invested = value * total_share
    # Cash has two sources and both must be reported: money not allocated to any operation
    # (``1 - total_share``), and money inside a sleeve held back to respect its risk budget
    # (``1 - risky_fraction``). Reporting only the first would show "0% cash" while a defensive
    # plan had a fifth of the account uninvested.
    invested_assets = sum(assets.values())
    return {
        'operations': rows,
        'assets': {a: round(w, 6) for a, w in sorted(assets.items()) if w > 1e-9},
        'invested': round(round(value * invested_assets, 2), 2),
        'cash': round(max(0.0, value * (1.0 - invested_assets)), 2),
        'account_value': round(value, 2),
        'portfolio_return': round(chosen['return'], 6),
        'portfolio_volatility': round(chosen['volatility'], 6),
        'prob_hit_goal': chosen['prob_hit_goal'],
        'drawdown_p95': chosen['dd_p95'],
        'regime': regime,
        'rf': rf,
        'basis': ('Expected returns are long-run priors, not forecasts. Volatility and the '
                  'correlation structure are assumptions, so treat the split as a disciplined '
                  'spread across horizons rather than a prediction.'),
    }


def build_plan(account_value=10000.0, asset_vols=None, asset_mu=None, regime_scale=1.0,
               goal_multiple=1.25, years=3.0, max_drawdown=0.25, rf=0.04,
               paths=2500, seed=11, operations=OPERATIONS, floors=None, caps=None,
               regime='uncertain'):
    """End-to-end: sleeve frontiers, account frontier across operations, MC choice, dollars."""
    ops = operation_inputs(operations, asset_vols, asset_mu, regime_scale, rf=rf)
    if not ops:
        return {'plan': None, 'operations': [], 'frontier': None, 'choice': None,
                'reason': 'no operations available'}
    cov = operation_covariance(ops)
    frontier = operations_frontier(ops, cov, floors, caps, rf=rf)
    choice = choose_distribution(frontier, goal_multiple, years, max_drawdown, paths, seed)
    plan = plan_account(ops, choice, account_value, rf, regime)
    return {'plan': plan, 'operations': ops, 'frontier': frontier, 'choice': choice,
            'covariance': cov}


def rebalance_toward(target_assets, current_assets, max_turnover=0.35):
    """Scale a target asset mix toward the current book within a turnover budget.

    Uses the same budget semantics as the rest of the codebase: a change larger than the budget
    is taken in instalments, never as a full liquidation.
    """
    from alpha_engine import apply_turnover_budget
    moved, turnover, partial = apply_turnover_budget(target_assets or {}, current_assets or {},
                                                     max_turnover)
    return {'weights': moved, 'turnover': round(turnover, 6), 'partial': partial,
            'cash': round(max(0.0, 1.0 - sum(moved.values())), 6)}
