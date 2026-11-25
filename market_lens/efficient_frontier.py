"""Efficient frontier and fast Monte Carlo for portfolio selection.

Two design rules, learned from this codebase's review findings:

* A cap is a cap. Long-only and per-sleeve caps are enforced by an active set, and the
  result is polished and checked (``kkt_residual``) rather than assumed correct.
* Fast Monte Carlo must sample the *portfolio*, not every asset on every path. A rebalanced
  book's log growth is normal with mean ``(mu - var/2) * T`` and standard deviation
  ``sqrt(var * T)``, so one draw per path is enough for terminal-wealth questions. A
  pathwise mode exists for drawdown questions, and is checked against the closed form in
  the tests.

Nothing here is a return forecast. ``expected returns`` are inputs (or documented priors
supplied by the caller); the frontier only shows what follows *if* they hold.
"""
import math
import random
import statistics

TOL = 1e-12


# --------------------------------------------------------------------------- linear algebra

def _inverse(m):
    """Gauss-Jordan inverse with partial pivoting. Returns None if singular."""
    n = len(m)
    a = [list(map(float, row)) + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-14:
            return None
        a[col], a[piv] = a[piv], a[col]
        p = a[col][col]
        a[col] = [x / p for x in a[col]]
        for r in range(n):
            if r != col and a[r][col] != 0.0:
                f = a[r][col]
                a[r] = [x - f * y for x, y in zip(a[r], a[col])]
    return [row[n:] for row in a]


def _matvec(m, v):
    return [sum(a * b for a, b in zip(row, v)) for row in m]


def _quad(w, m):
    return sum(wi * x for wi, x in zip(w, _matvec(m, w)))


def _sub(m, rows, cols):
    return [[m[i][j] for j in cols] for i in rows]


def _diag_ridge(m, eps):
    n = len(m)
    return [[m[i][j] + (eps if i == j else 0.0) for j in range(n)] for i in range(n)]


def _is_finite_matrix(m):
    return bool(m) and all(
        isinstance(x, (int, float)) and math.isfinite(x) for row in m for x in row)


def covariance_from_vols(vols, corr=0.0, pairs=None):
    """Build a covariance matrix from per-asset volatilities and a correlation structure.

    ``corr`` is the default pairwise correlation; ``pairs`` may override individual pairs as
    ``{(a, b): rho}``. Keys are sorted so the output order is deterministic.
    """
    names = sorted(v for v in vols if _positive(vols[v]))
    n = len(names)
    m = [[0.0] * n for _ in range(n)]
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            if i == j:
                m[i][j] = float(vols[a]) ** 2
            else:
                rho = _pair_corr(a, b, pairs, corr)
                m[i][j] = rho * float(vols[a]) * float(vols[b])
    return names, m


def _pair_corr(a, b, pairs, default):
    if not pairs:
        return max(-1.0, min(1.0, float(default)))
    for key in ((a, b), (b, a), (f'{a}|{b}',), (f'{b}|{a}',)):
        if isinstance(pairs, dict):
            for k in (key if isinstance(key, tuple) else key,):
                if k in pairs:
                    rho = _num(pairs[k])
                    if rho is not None:
                        return max(-1.0, min(1.0, rho))
    return max(-1.0, min(1.0, float(default)))


def covariance_from_history(series_map, min_obs=8, shrinkage=0.10):
    """Sample covariance from aligned return histories, shrunk toward its diagonal.

    Shrinkage keeps the matrix invertible on short histories, which is what the frontier
    solve needs. Series with too few observations or no variation are dropped, and the
    caller is told which ones survived.
    """
    clean = {}
    for k, v in (series_map or {}).items():
        vals = [_num(x) for x in (v or [])]
        vals = [x for x in vals if x is not None]
        if len(vals) >= min_obs + 1:
            rets = [b / a - 1.0 for a, b in zip(vals, vals[1:]) if a > 0]
            if len(rets) >= min_obs and statistics.pstdev(rets) > 1e-12:
                clean[k] = rets
    names = sorted(clean)
    n = len(names)
    if n == 0:
        return names, []
    k = min(len(clean[s]) for s in names)
    used = {s: clean[s][-k:] for s in names}
    means = {s: statistics.mean(used[s]) for s in names}
    m = [[0.0] * n for _ in range(n)]
    for i, a in enumerate(names):
        for j, b in enumerate(names):
            cov = sum((x - means[a]) * (y - means[b]) for x, y in zip(used[a], used[b])) / max(k - 1, 1)
            m[i][j] = cov
    return names, shrink_to_diagonal(m, shrinkage)


def shrink_to_diagonal(m, shrinkage=0.10):
    """Shrink off-diagonal covariance toward zero, keeping variances. Never creates a bad matrix."""
    n = len(m)
    if n == 0:
        return []
    lam = max(0.0, min(1.0, float(shrinkage)))
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            out[i][j] = m[i][i] if i == j else lam * m[i][j]
    return out


def annualise(cov, periods_per_year=252):
    return [[x * periods_per_year for x in row] for row in cov]


def portfolio_stats(weights, names, mu, cov):
    """Return and volatility of a named weight vector. Missing names contribute nothing."""
    w = [float(weights.get(n, 0.0)) for n in names]
    ret = sum(a * b for a, b in zip(w, mu))
    var = max(0.0, _quad(w, cov))
    return {'return': ret, 'volatility': math.sqrt(var), 'variance': var,
            'sharpe': (ret - 0.0) / math.sqrt(var) if var > 0 else None}


# --------------------------------------------------------------------------- constraints

def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _positive(x):
    v = _num(x)
    return v is not None and v > 0


def _caps_from_names(names, caps):
    """Resolve a cap spec given asset names. A dict is keyed by name; None means uncapped."""
    if isinstance(caps, dict):
        return [min(1.0, max(0.0, _num(caps.get(n, 1.0)) or 0.0)) for n in names]
    return _cap_list(caps, len(names))


def _equality_solution(mu, cov, free, fixed, budget, target):
    """Exact solution of the free-block KKT system, with pinned assets held at their bounds.

    The optimality condition on the free assets is ``(Sigma w)_i = u`` for the minimum-variance
    problem, or ``(Sigma w)_i = u + v*mu_i`` when a return target applies. Splitting ``w`` into
    free and fixed blocks gives

        ``w_F = Sigma_FF^-1 (u*1 + v*mu_F - Sigma_FC c_C)``

    and the budget and return constraints then determine ``u`` and ``v`` by a 2x2 solve. This is
    exact: one linear solve per active set, no descent steps.

    ``budget`` is the total that the free assets must sum to (the budget not already consumed by
    pinned assets). ``target`` is the residual return requirement on the free block.
    """
    k = len(free)
    if k == 0:
        return None
    inv = _inverse(_diag_ridge(_sub(cov, free, free), 1e-14))
    if inv is None:
        return None
    ones = [1.0] * k
    s1 = _matvec(inv, ones)
    a11 = sum(s1)
    if a11 <= 1e-18:
        return None
    # Contribution of assets already pinned at a bound.
    if fixed:
        g = [sum(cov[i][j] * v for j, v in fixed.items()) for i in free]
        sinv_g = _matvec(inv, g)
    else:
        sinv_g = [0.0] * k
    c1 = sum(sinv_g)
    if target is None:
        u = (budget + c1) / a11
        return [u * s1[i] - sinv_g[i] for i in range(k)]
    muf = [mu[i] for i in free]
    sm = _matvec(inv, muf)
    a12 = sum(sm)
    a22 = sum(muf[i] * sm[i] for i in range(k))
    c2 = sum(muf[i] * sinv_g[i] for i in range(k))
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-18:
        return None
    # u*a11 + v*a12 = budget + c1   and   u*a12 + v*a22 = target + c2
    u = ((budget + c1) * a22 - (target + c2) * a12) / det
    v = ((target + c2) * a11 - (budget + c1) * a12) / det
    return [u * s1[i] + v * sm[i] - sinv_g[i] for i in range(k)]


def _cap_list(caps, n):
    """Resolve a cap spec into a per-index list. ``None`` means 1.0 (uncapped)."""
    if caps is None:
        return [1.0] * n
    if isinstance(caps, (list, tuple)):
        out = [min(1.0, max(0.0, _num(x) or 0.0)) for x in caps]
        return (out + [1.0] * n)[:n]
    single = _num(caps)
    if single is None:
        return [1.0] * n
    return [min(1.0, max(0.0, single))] * n


def min_variance_weights(mu, cov, target=None, caps=None, budget=1.0, max_iter=None):
    """Long-only, capped minimum-variance weights, optionally at a target return.

    Solved by an exact active-set method for the convex quadratic program. At the optimum the
    marginal variance ``(Sigma w)_i`` is equal across every asset that is strictly inside its
    bounds, is not below that level for assets pinned at zero, and not above it for assets
    pinned at their cap.

    Given the current active set, ``Sigma_FF w_F = theta/2 * 1 - Sigma_FC c_C`` makes that
    condition hold exactly on the free assets, and the budget condition fixes ``theta``. So each
    iteration is a single linear solve, not a descent step. An asset the solve pushes below zero
    is pinned at zero; one pushed above its cap is pinned at the cap; repeat.

    ``target`` adds the return constraint ``mu'w = target``, which fixes the second multiplier.

    ``budget`` is how much of the portfolio may be invested. Letting it be lower than one is how
    a caller expresses cash: whatever caps cannot place is simply not invested.

    Returns ``{'weights', 'feasible', 'kkt_residual', 'invested', 'cash', 'reason'}``. Every
    answer is checked against the optimality conditions, so an approximate result is reported as
    infeasible with a reason rather than presented as optimal.
    """
    mu = [float(x) for x in mu]
    n = len(mu)
    if n == 0 or len(cov) != n:
        return {'weights': [], 'feasible': False, 'kkt_residual': None, 'reason': 'empty problem'}
    cap = _cap_list(caps, n)
    budget = _num(budget)
    if budget is None or budget <= 0:
        return {'weights': [0.0] * n, 'feasible': False, 'kkt_residual': None,
                'reason': 'budget must be positive'}
    budget = min(1.0, budget)
    placeable = min(budget, sum(cap))
    if placeable <= TOL:
        return {'weights': [0.0] * n, 'feasible': False, 'kkt_residual': None,
                'reason': 'caps sum to zero'}
    short = placeable < budget - 1e-9
    free = list(range(n))
    fixed = {}
    w = None
    reason = ''
    tgt = None if target is None else float(target)
    # Each pass pins at least one asset at a bound, so 2n passes is more than enough.
    for _ in range(max_iter or (2 * n + 4)):
        left = placeable - sum(fixed.values())
        if left <= TOL or not free:
            w = [fixed.get(i, 0.0) for i in range(n)]
            break
        residual_target = None
        if tgt is not None:
            residual_target = tgt - sum(mu[i] * fixed.get(i, 0.0) for i in fixed)
        if len(free) == 1:
            # One free asset: the budget alone determines it; the return is whatever it gives.
            sol = [left]
        else:
            sol = _equality_solution(mu, cov, free, fixed, left, residual_target)
        if sol is None or not all(math.isfinite(x) for x in sol):
            w = None
            reason = 'singular covariance on the free set'
            break
        neg = [(i, sol[j]) for j, i in enumerate(free) if sol[j] < -1e-11]
        over = [(i, sol[j]) for j, i in enumerate(free) if sol[j] > cap[i] + 1e-11]
        if neg:
            # Pin the worst offender: largest negative magnitude.
            i = min(neg, key=lambda x: x[1])[0]
            fixed[i] = 0.0
            free.remove(i)
            continue
        if over:
            i = max(over, key=lambda x: x[1] - cap[x[0]])[0]
            fixed[i] = cap[i]
            free.remove(i)
            continue
        w = [0.0] * n
        for j, i in enumerate(free):
            w[i] = sol[j]
        for i, v in fixed.items():
            w[i] = v
        break
    if w is None:
        return {'weights': [0.0] * n, 'feasible': False, 'kkt_residual': None,
                'reason': reason or 'no feasible solution'}
    w = _clip_clean(w, cap)
    total = sum(w)
    residual = _kkt_residual(w, mu, cov, cap, target)
    return_ok = tgt is None or abs(sum(a * b for a, b in zip(w, mu)) - tgt) <= 1e-6
    feasible = (abs(total - placeable) <= 1e-6 and all(x >= -1e-9 for x in w)
                and all(w[i] <= cap[i] + 1e-6 for i in range(n))
                and residual is not None and residual <= 0.05 and return_ok)
    if not feasible and not reason:
        reason = 'caps cannot reach the budget' if short else 'solution not optimal under caps'
    return {'weights': w, 'feasible': feasible, 'kkt_residual': residual,
            'invested': total, 'cash': max(0.0, 1.0 - total),
            'reason': '' if feasible else reason}


def _clip_clean(w, cap):
    """Remove floating-point dust only. Never rescales, so a cap cannot be broken here."""
    return [0.0 if x < 1e-12 else min(cap[i], x) for i, x in enumerate(w)]



def _kkt_residual(w, mu, cov, cap, target=None):
    """Relative distance from the optimality conditions for the retrieved solution.

    For a min-variance problem the gradient of ``w'Σw`` must be equal across every free asset.
    For a target-return problem it must be an affine function of ``μ``, so ``g - λ2·μ`` is
    constant over the free set; λ2 is fitted by least squares. Assets pinned at a bound must have
    a gradient on the correct side of that common level.
    """
    n = len(w)
    if n == 0:
        return None
    g = _matvec(cov, w)
    at_zero = [i for i in range(n) if w[i] <= 1e-9]
    at_cap = [i for i in range(n) if w[i] >= cap[i] - 1e-9]
    free = [i for i in range(n) if i not in at_zero and i not in at_cap]
    scale = max(1e-12, max(abs(x) for x in g))
    if not free:
        return 0.0
    if target is not None:
        # Fit g_i = a + b*mu_i on the free set; the residual is what must vanish.
        xs = [mu[i] for i in free]
        ys = [g[i] for i in free]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den > 1e-18 else 0.0
        adj = [g[i] - b * mu[i] for i in range(n)]
    else:
        adj = g
    gm = sum(adj[i] for i in free) / len(free)
    spread = max(abs(adj[i] - gm) for i in free) / scale
    below = max((gm - adj[i] for i in at_zero), default=0.0) / scale
    above = max((adj[i] - gm for i in at_cap), default=0.0) / scale
    return float(max(spread, below, above))


# --------------------------------------------------------------------------- frontier

def efficient_frontier(mu, cov, names=None, rf=0.0, points=25, caps=None):
    """Compute the long-only efficient frontier.

    The budget placed in risky assets is ``min(1, sum(caps))``: if caps cannot hold 100%, the
    rest is cash and is reported as ``cash`` on every point. Return is therefore an allocation
    return, so comparing two points also compares their exposure, which is what the caller needs.

    Every point carries a feasibility flag and a KKT residual, so a constrained result is never
    presented as if it were the unconstrained one.
    """
    mu = [float(x) for x in mu]
    n = len(mu)
    names = list(names or [str(i) for i in range(n)])
    if n == 0 or len(cov) != n:
        return {'names': [], 'points': [], 'min_variance': None, 'max_return': None,
                'tangency': None, 'rf': rf, 'cash': 1.0}
    cap = _caps_from_names(names, caps)
    budget = min(1.0, sum(cap))
    cash = max(0.0, 1.0 - budget)
    gmv = min_variance_weights(mu, cov, None, cap, budget=budget)
    if not gmv['feasible']:
        return {'names': names, 'points': [], 'min_variance': None, 'max_return': None,
                'tangency': None, 'rf': rf, 'cash': cash,
                'reason': gmv['reason'] or 'no feasible minimum-variance portfolio'}
    lo = portfolio_stats(dict(zip(names, gmv['weights'])), names, mu, cov)['return']
    hi_w, hi_invested = _max_return_weights(mu, cap, budget)
    hi = sum(a * b for a, b in zip(hi_w, mu))

    def point(st, weights, invested, kkt, target=None):
        return {'target_return': None if target is None else round(target, 8),
                'return': st['return'], 'volatility': st['volatility'],
                'sharpe': (st['return'] - rf) / st['volatility'] if st['volatility'] > 0 else None,
                'weights': dict(zip(names, weights)),
                'invested': round(invested, 9), 'cash': round(1.0 - invested, 9),
                'kkt_residual': kkt}

    pts = [point(portfolio_stats(dict(zip(names, gmv['weights'])), names, mu, cov),
                 gmv['weights'], gmv['invested'], gmv['kkt_residual'])]
    seen = {tuple(round(x, 6) for x in gmv['weights'])}
    # Only interior targets: a target equal to an endpoint makes the sub-problem degenerate.
    if hi - lo > 1e-9 and points > 2:
        span = hi - lo
        for i in range(1, points - 1):
            target = lo + span * i / (points - 1)
            r = min_variance_weights(mu, cov, target, cap, budget=budget)
            if not r['feasible']:
                continue
            key = tuple(round(x, 6) for x in r['weights'])
            if key in seen:
                continue
            seen.add(key)
            st = portfolio_stats(dict(zip(names, r['weights'])), names, mu, cov)
            pts.append(point(st, r['weights'], r['invested'], r['kkt_residual'], target))
    hi_var = _quad(hi_w, cov)
    hi_st = {'return': hi, 'volatility': math.sqrt(max(0.0, hi_var))}
    hi_kkt = _kkt_residual(hi_w, mu, cov, cap)
    pts.append(point(hi_st, hi_w, hi_invested, hi_kkt))
    pts.sort(key=lambda p: (p['volatility'], p['return']))
    tangency = max((p for p in pts if p['sharpe'] is not None),
                   key=lambda p: p['sharpe'], default=None)
    return {
        'names': names,
        'rf': rf,
        'invested_fraction': round(sum(gmv['weights']), 9),
        'cash': round(cash, 9),
        'min_variance': _point(gmv, names, mu, cov, rf),
        'max_return': {'return': round(hi, 9), 'weights': dict(zip(names, hi_w)),
                       'invested': round(hi_invested, 9), 'cash': round(1.0 - hi_invested, 9),
                       'volatility': round(hi_st['volatility'], 9), 'kkt_residual': hi_kkt},
        'tangency': tangency,
        'points': pts,
    }


def _point(res, names, mu, cov, rf):
    if not res['feasible']:
        return None
    st = portfolio_stats(dict(zip(names, res['weights'])), names, mu, cov)
    return {'return': st['return'], 'volatility': st['volatility'],
            'sharpe': (st['return'] - rf) / st['volatility'] if st['volatility'] > 0 else None,
            'weights': dict(zip(names, res['weights'])),
            'invested': res['invested'], 'cash': res['cash'],
            'kkt_residual': res['kkt_residual']}


def _max_return_weights(mu, cap, budget=1.0):
    """Greedy fill by expected return, respecting caps. Returns (weights, invested)."""
    order = sorted(range(len(mu)), key=lambda i: -mu[i])
    w = [0.0] * len(mu)
    left = budget
    for i in order:
        if left <= 1e-12:
            break
        w[i] = max(0.0, min(cap[i], left))
        left -= w[i]
    return w, budget - max(0.0, left)


def portfolio_at_vol(mu, cov, names, target_vol, rf=0.0, caps=None, points=120):
    """The highest-return frontier point whose volatility does not exceed ``target_vol``."""
    f = efficient_frontier(mu, cov, names, rf=rf, caps=caps, points=points)
    ok = [p for p in f['points'] if p['volatility'] <= target_vol + 1e-12]
    if not ok:
        return f.get('min_variance')
    return max(ok, key=lambda p: (p['return'], -p['volatility']))


# --------------------------------------------------------------------------- fast Monte Carlo

def fast_monte_carlo(mu, volatility, years=1.0, paths=20000, goal=None, start=1.0, seed=7):
    """Terminal-wealth distribution by the closed-form log-normal shortcut.

    For a continuously rebalanced book the log growth is normal with mean
    ``(mu - var/2) * T`` and variance ``var * T``, so one normal draw per path is exact for
    terminal wealth. That is O(paths) instead of O(paths * assets * steps), and it uses
    antithetic pairs so the estimate is symmetric and lower-variance.
    """
    m = _num(mu)
    v = _num(volatility)
    if m is None or v is None or v < 0 or paths < 1:
        return {'ok': False, 'reason': 'invalid mu/vol/paths'}
    years = max(1e-9, float(years))
    n = max(2, int(paths))
    rng = random.Random(seed)
    drift = (m - 0.5 * v * v) * years
    sd = v * math.sqrt(years)
    half = n // 2
    zs = [rng.gauss(0.0, 1.0) for _ in range(half)]
    zs = zs + [-z for z in zs] + ([0.0] if n % 2 else [])
    wealth = sorted(start * math.exp(drift + sd * z) for z in zs)
    return _summarise(wealth, start, goal, years)


def simulate_paths(mu, volatility, years=1.0, paths=5000, steps=12, goal=None, start=1.0, seed=7):
    """Pathwise simulation, for questions the closed form cannot answer (drawdown, path goals).

    Draws one scalar return per step, not one per asset: the portfolio is already aggregated,
    which is the same speed-up the frontier provides upstream.
    """
    m = _num(mu)
    v = _num(volatility)
    if m is None or v is None or v < 0 or paths < 1:
        return {'ok': False, 'reason': 'invalid mu/vol/paths'}
    years = max(1e-9, float(years))
    steps = max(1, int(steps))
    dt = years / steps
    rng = random.Random(seed)
    mu_s, sd_s = m * dt, v * math.sqrt(dt)
    finals, dds, hit = [], [], 0
    n = max(2, int(paths))
    for _ in range(n):
        w = start
        peak = w
        worst = 0.0
        for _ in range(steps):
            w *= math.exp(mu_s - 0.5 * sd_s * sd_s + sd_s * rng.gauss(0.0, 1.0))
            peak = max(peak, w)
            worst = max(worst, 1.0 - w / peak)
        finals.append(w)
        dds.append(worst)
        if goal is not None and w >= goal:
            hit += 1
    finals.sort()
    out = _summarise(finals, start, goal, years)
    out['max_drawdown'] = {
        'median': round(statistics.median(dds), 6),
        'p95': round(sorted(dds)[min(len(dds) - 1, int(0.95 * len(dds)))], 6),
        'prob_over_10pct': round(sum(1 for d in dds if d > 0.10) / len(dds), 4),
    }
    out['method'] = 'pathwise'
    return out


def _summarise(wealth, start, goal, years):
    n = len(wealth)
    def q(p):
        return wealth[min(n - 1, max(0, int(p * n)))]
    out = {
        'ok': True,
        'paths': n,
        'years': years,
        'median_wealth': round(statistics.median(wealth), 6),
        'mean_wealth': round(statistics.mean(wealth), 6),
        'p05': round(q(0.05), 6),
        'p95': round(q(0.95), 6),
        'prob_loss': round(sum(1 for w in wealth if w < start) / n, 4),
        'method': 'closed-form',
    }
    if goal is not None:
        out['goal'] = goal
        out['prob_hit_goal'] = round(sum(1 for w in wealth if w >= goal) / n, 4)
    return out


def choose_distribution(candidates, goal=None, max_drawdown=None, start=1.0, years=1.0,
                        paths=8000, seed=7):
    """Pick the candidate allocation that is *most likely* to meet the goal without breaching risk.

    ``candidates`` are dicts with ``label``, ``mu`` and ``volatility``. Selection order:

    1. discard candidates whose simulated 95th-percentile drawdown exceeds ``max_drawdown``;
    2. among survivors, prefer the highest probability of reaching ``goal``;
    3. break ties by higher median terminal wealth, then lower volatility.

    If nothing survives the drawdown limit, the lowest-drawdown candidate is returned with a
    warning rather than an empty answer.
    """
    rows = []
    for c in candidates or []:
        mu, vol = _num(c.get('mu')), _num(c.get('volatility'))
        if mu is None or vol is None:
            continue
        sim = simulate_paths(mu, vol, years=years, paths=paths, goal=goal, start=start, seed=seed)
        if not sim.get('ok'):
            continue
        rows.append({'label': c.get('label'), 'mu': mu, 'volatility': vol,
                     'prob_hit_goal': sim.get('prob_hit_goal'),
                     'median_wealth': sim['median_wealth'],
                     'dd_p95': sim['max_drawdown']['p95'],
                     'dd_median': sim['max_drawdown']['median'],
                     'weights': c.get('weights')})
    if not rows:
        return {'chosen': None, 'candidates': [], 'reason': 'no usable candidates'}
    limit = _num(max_drawdown)
    ok = [r for r in rows if limit is None or (r['dd_p95'] or 0.0) <= limit] if limit is not None else rows
    warn = None
    if not ok:
        ok = [min(rows, key=lambda r: (r['dd_p95'] or 0.0))]
        warn = 'no candidate met the drawdown limit; returned the least risky'
    chosen = max(ok, key=lambda r: (
        r['prob_hit_goal'] if r['prob_hit_goal'] is not None else 0.0,
        r['median_wealth'], -r['volatility']))
    return {'chosen': chosen, 'candidates': rows, 'warning': warn}
