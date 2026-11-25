"""Read-only plan and supervisor views for the local monitor.

The dashboard opens the journal with ``pragma query_only``, so this module must not write. It
builds a strategy decision from the stored data, runs the supervisor assessment, and returns the
operations ladder. Anything expensive is bounded by the query-string limits so a stray request
cannot make the monitor slow.

All parameters arrive as text from the URL, so every one is parsed strictly and an invalid value
raises ``ValueError`` (which the server turns into a 400) instead of silently falling back.
"""

MAX_ACCOUNT = 1e12
MAX_PATHS = 4000


def _f(raw, name, default, lo=None, hi=None):
    if raw in (None, ''):
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError('%s must be a number' % name) from None
    if v != v or v in (float('inf'), float('-inf')):
        raise ValueError('%s must be finite' % name)
    if lo is not None and v < lo:
        raise ValueError('%s must be at least %s' % (name, lo))
    if hi is not None and v > hi:
        raise ValueError('%s must be at most %s' % (name, hi))
    return v


def _vols(raw):
    """Parse ``SPY=0.16,TLT=0.13`` into a volatility map. Empty means "use the priors"."""
    out = {}
    for item in (raw or '').split(','):
        item = item.strip()
        if not item:
            continue
        name, _, value = item.partition('=')
        name = name.strip().upper()
        if not name or not value:
            raise ValueError('vol expects ASSET=VOL pairs')
        v = _f(value, 'vol', None, 0.0, 5.0)
        if v <= 0:
            raise ValueError('vol must be positive')
        out[name] = v
    return out


def plan_view(c, home, arg, path='/api/plan'):
    """Build the plan (and, for ``/api/jarvis``, the supervisor view) from the stored journal."""
    from strategy_engine import decide
    from portfolio_optimizer import build_plan
    from jarvis_supervisor import assess_change, jarvis_briefing, recommended_book

    vols = _vols(arg('vol'))
    account = _f(arg('account'), 'account', 10000.0, 0.0, MAX_ACCOUNT)
    goal = _f(arg('goal'), 'goal', 1.25, 1e-6, 100.0)
    years = _f(arg('years'), 'years', 3.0, 1e-6, 60.0)
    max_dd = _f(arg('max_dd'), 'max_dd', 0.25, 1e-6, 1.0)
    target_vol = _f(arg('target_vol'), 'target_vol', 0.10, 1e-6, 2.0)
    risk_scale = arg('regime_scale')
    risk_scale = None if risk_scale in (None, '') else _f(risk_scale, 'regime_scale', 1.0, 0.0, 1.0)

    decision = decide(c, None, vols, target_vol=target_vol)
    scale = risk_scale if risk_scale is not None else decision.get('risk_scale', 1.0)
    built = build_plan(account_value=account, asset_vols=vols or None, regime_scale=scale,
                       goal_multiple=goal, years=years, max_drawdown=max_dd,
                       regime=decision.get('regime', 'uncertain'),
                       rf=decision.get('risk_free_rate') or 0.04)
    plan = built.get('plan')
    frontier = built.get('frontier') or {}
    out = {
        'asof_utc': decision.get('asof_utc'),
        'account_value': account,
        'regime': decision.get('regime'),
        'risk_scale': scale,
        'risk_free_rate': decision.get('risk_free_rate'),
        'decision': {k: decision.get(k) for k in (
            'regime', 'regime_confirmed', 'quadrant', 'target_weights', 'booked_weights',
            'cash', 'portfolio_vol_estimate', 'target_vol', 'inputs_used', 'guard')},
        'plan': plan,
        'operations': built.get('operations') or [],
        'frontier': {
            'min_variance': frontier.get('min_variance'),
            'tangency': frontier.get('tangency'),
            'cash': frontier.get('cash'),
            'points': [{'volatility': p['volatility'], 'return': p['return'],
                        'sharpe': p['sharpe']} for p in (frontier.get('points') or [])],
        },
        'choice': {
            'warning': (built.get('choice') or {}).get('warning'),
            'evaluated': (built.get('choice') or {}).get('evaluated'),
        },
        'disclaimer': ('Decision support only. Expected returns are long-run priors, not '
                       'forecasts, and no broker is contacted.'),
    }
    if path == '/api/jarvis':
        status_dict = None
        try:
            from engine import status
            status_dict = status(c, home)
        except Exception:
            status_dict = {'worker_online': False}
        assessment = assess_change(decision, None, max_drawdown=max_dd)
        out['briefing'] = jarvis_briefing(status_dict, decision)
        out['assessment'] = assessment
        out['next_book'] = recommended_book(assessment, decision)
    return out
