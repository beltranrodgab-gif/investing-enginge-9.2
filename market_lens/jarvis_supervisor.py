"""J.A.R.V.I.S. Supervisor: watches the system and the book, and says when a change is needed.

The module was previously dead code: nothing imported it, so its briefing never ran. It is now
wired in three places -- ``strategy`` embeds it, ``market_lens.py jarvis`` prints it and
``/api/jarvis`` serves it.

The important part is ``change_assessment``. A supervisor that only prints "nominal" is
decoration; this one has to answer "should the book change, and why?". It compares the strategy's
own state against a stored previous state and raises a trigger when any of the following is true:

* the macro regime changed;
* the target book differs from the current book by more than a drift tolerance;
* the portfolio's estimated volatility is outside the band around its target;
* drawdown passed a hard limit;
* feeds are failing, or the worker is offline, so the evidence is stale.

Each trigger carries a severity and a plain-language reason. The assessment also returns the
recommended next book, already scaled by ``portfolio_optimizer.rebalance_toward`` so a large
change is taken in instalments under the turnover budget rather than all at once.
"""
import json

from macro_alerts import detect_macro_chains
from strategy_adapter import load_screened_signals

# Keyed by the names ``alpha_engine.detect_regime`` actually returns. The old table used
# ``'normal'`` for the expansion case while the detector returned ``'normal_expansion'``, so a
# healthy economy fell through to the *unresolved* outlook, and the risk table's
# ``'uncertain': 1.0`` contradicted the detector's fail-closed 0.5 for an unknown regime. Both are
# now derived from one source, so the supervisor cannot disagree with the detector.
REGIME_META = {
    'normal_expansion': (1.0, 'Normal expansion: favour equities and growth while monitoring '
                              'the yield curve.'),
    'defensive_inverted_curve': (0.5, 'Inverted yield curve: elevated recession risk over the '
                                      'next 6-12 months; favour Treasuries, gold and T-bills.'),
    'tight_liquidity': (0.75, 'Tight liquidity: elevated funding rates may slow growth; risks '
                              'scaled to 75%.'),
    'uncertain': (0.5, 'Regime unresolved: risk halved until the evidence resolves, rather than '
                       'assuming a benign environment.'),
}

REGIME_OUTLOOK = {k: v[1] for k, v in REGIME_META.items()}
REGIME_RISK = {k: v[0] for k, v in REGIME_META.items()}
# An unrecognised label is unknown, and unknown is defensive. This is the same fail-closed default
# ``detect_regime`` uses, expressed once as a named constant.
UNKNOWN_RISK = REGIME_META['uncertain'][0]


def _num(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v


def jarvis_briefing(status_dict, current_proc=None, csv_path=None):
    """Structured supervisor briefing covering health, operations and the forward outlook.

    Important distinction, and the bug the old version had: ``accepted_feeds`` counts feeds that
    passed *in the current worker session*, and it is zero whenever the worker is stopped. That
    is a process fact, not a data outage. Reporting "26 feed(s) failing" while every stored
    series is fresh would contradict the strategy, which reads the stored journal and can still
    see dozens of usable inputs. So the two are kept apart: worker state and data freshness.
    """
    status_dict = status_dict or {}
    current_proc = current_proc or {}
    online = bool(status_dict.get('worker_online'))
    ok_feeds = status_dict.get('accepted_feeds', 0) or 0
    tot_feeds = status_dict.get('enabled_feeds', 0) or 0
    warnings = []
    if not online:
        # Not a data failure: the stored journal is unchanged and still readable.
        warnings.append('Worker stopped: no new checks are running; decisions use the stored '
                        'journal, and stale rows fail closed.')
    elif tot_feeds and ok_feeds < tot_feeds:
        warnings.append('%d feed(s) pending or retrying in this session.'
                        % (tot_feeds - ok_feeds))
    regime = current_proc.get('regime', 'uncertain')
    outlook = REGIME_OUTLOOK.get(regime, REGIME_OUTLOOK['uncertain'])
    chains = []
    if csv_path:
        try:
            sigs = load_screened_signals(csv_path)
            chains = detect_macro_chains(sigs)
        except Exception:
            # A briefing must not die because an export is unreadable; it reports the gap instead.
            warnings.append('Macro-chain detection skipped: export unreadable.')
    return {
        'status': 'NOMINAL' if not warnings else 'ATTENTION',
        'worker': 'running' if online else 'stopped',
        'feeds': ('%d/%d passing this session' % (ok_feeds, tot_feeds) if online
                  else 'worker stopped, %d series in journal' % len(current_proc.get('inputs_used') or [])),
        'usable_inputs': len(current_proc.get('inputs_used') or []),
        'warnings': warnings,
        'outlook': outlook,
        'regime': regime,
        'chains': chains,
    }


def assess_change(current, previous=None, risk_tolerance=0.20, drift_tolerance=0.10,
                  max_drawdown=0.15):
    """Decide whether the book needs to change, and say exactly why.

    ``current`` is a strategy decision from ``strategy_engine.decide``; ``previous`` is the
    prior state, either a decision or a small dict with ``regime``/``weights``. Everything is
    optional, so the check runs on a fresh install.

    Triggers are ordered by severity: ``halt`` (evidence unusable), ``change`` (a real reason to
    rebalance), ``watch`` (worth noting). An empty ``triggers`` list means "no action".
    """
    current = current or {}
    previous = previous or {}
    triggers = []

    def add(kind, severity, reason):
        triggers.append({'kind': kind, 'severity': severity, 'reason': reason})

    regime = current.get('regime', 'uncertain')
    prior_regime = previous.get('regime')
    if prior_regime and prior_regime != regime:
        add('regime_change', 'change',
            'Regime moved from %s to %s.' % (prior_regime, regime))

    if not current.get('inputs_used'):
        add('feed_loss', 'halt',
            'No usable macro input: target is cash until evidence returns.')

    confirmed = current.get('regime_confirmed')
    if confirmed and confirmed != regime:
        add('regime_unconfirmed', 'watch',
            'Regime %s is not confirmed by recent history (%s).' % (regime, confirmed))

    # Drift between where the book is and where the model wants it.
    target = current.get('target_weights') or {}
    booked = current.get('booked_weights') or previous.get('weights') or {}
    drift = _l1(target, booked)
    if drift > drift_tolerance:
        add('target_drift', 'change',
            'Target book differs from the current book by %.1f%% (limit %.0f%%).'
            % (drift * 100, drift_tolerance * 100))

    vol = _num(current.get('portfolio_vol_estimate'))
    target_vol = _num(current.get('target_vol'))
    if vol is not None and target_vol:
        if vol > target_vol * (1 + risk_tolerance):
            add('over_risk', 'change',
                'Estimated volatility %.1f%% is above the %.1f%% budget by more than %.0f%%.'
                % (vol * 100, target_vol * 100, risk_tolerance * 100))
    # ``strategy_engine.decide`` reports the drawdown as ``drawdown_pct``; reading only
    # ``drawdown`` meant this trigger could never fire on a real decision. Both names are accepted.
    dd = _num(current.get('drawdown_pct'))
    if dd is None:
        dd = _num(current.get('drawdown'))
    if dd is not None and dd > max_drawdown:
        add('drawdown_breach', 'change',
            'Drawdown %.1f%% exceeded the %.1f%% limit.' % (dd * 100, max_drawdown * 100))
    elif dd is None and 'drawdown_pct' in current:
        # A decision that carries the key but no value means a curve *was* offered and nothing in
        # it was usable. That is worth saying: the limit could not be checked on evidence that
        # looked like it was there. A caller that never supplies a curve at all is not nagged.
        add('drawdown_unknown', 'watch',
            'Equity curve supplied but unusable, so the drawdown limit could not be checked.')

    if current.get('partial_rebalance'):
        add('partial_rebalance', 'watch',
            'Last rebalance was capped by the turnover budget and continues next period.')
    for issue in current.get('budget_issues') or []:
        add('budget', 'watch', str(issue))
    if current.get('guard'):
        add('guard', 'watch', str(current['guard']))

    severity = 'halt' if any(t['severity'] == 'halt' for t in triggers) else (
        'change' if any(t['severity'] == 'change' for t in triggers) else (
            'watch' if triggers else 'none'))
    return {
        'action': 'hold' if severity in ('none', 'watch') else 'rebalance',
        'severity': severity,
        'triggers': triggers,
        'drift': round(drift, 4),
        'regime': regime,
        'risk_scale': REGIME_RISK.get(regime, UNKNOWN_RISK),
    }


def _l1(a, b):
    keys = set(a) | set(b)
    return sum(abs(_num(a.get(k), 0.0) - _num(b.get(k), 0.0)) for k in keys)


def recommended_book(assessment, current, previous_weights=None, max_turnover=0.35):
    """The next book, applied in instalments when the change exceeds the turnover budget."""
    from portfolio_optimizer import rebalance_toward
    target = (current or {}).get('target_weights') or {}
    if not assessment or assessment.get('action') == 'hold':
        return {'weights': dict(previous_weights or current.get('booked_weights') or {}),
                'turnover': 0.0, 'partial': False, 'cash': 0.0, 'unchanged': True}
    current_book = previous_weights or (current or {}).get('booked_weights') or {}
    return rebalance_toward(target, current_book, max_turnover)


def supervisor_view(status_dict, decision, plan=None, previous=None, **kw):
    """One call that a CLI or HTTP route can serve: briefing plus change assessment plus plan."""
    assessment = assess_change(decision, previous, **kw)
    out = {
        'briefing': jarvis_briefing(status_dict, decision),
        'assessment': assessment,
        'next_book': recommended_book(assessment, decision, (previous or {}).get('weights')),
    }
    if plan is not None:
        out['plan'] = plan
    return out


def load_previous_state(path):
    """Read the last saved strategy state. A corrupt file is ignored, not fatal."""
    if not path:
        return {}
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path, decision):
    """Persist the minimum needed to compare against the next run."""
    if not path:
        return
    state = {
        'regime': (decision or {}).get('regime'),
        'regime_confirmed': (decision or {}).get('regime_confirmed'),
        'weights': (decision or {}).get('booked_weights') or {},
        'asof_utc': (decision or {}).get('asof_utc'),
    }
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
    except OSError:
        pass


def generate_report(period, history, briefing):
    """Generate a daily, weekly, monthly or yearly executive report."""
    window = {'daily': 1, 'weekly': 7, 'monthly': 30, 'yearly': 365}.get(period, 1)
    slice_h = history[-window:] if history else []
    avg_risk = (round(sum(_num(h.get('risk_scale'), 0.0) for h in slice_h) / len(slice_h), 4)
                if slice_h else None)
    regimes = [h.get('regime') for h in slice_h if h.get('regime')]
    top_regime = max(set(regimes), key=regimes.count) if regimes else 'n/a'
    return {
        'company': 'Obsidian Economics',
        'period': period,
        'decisions_count': len(slice_h),
        'dominant_regime': top_regime,
        'avg_risk_scale': avg_risk,
        'status': (briefing or {}).get('status', 'UNKNOWN'),
        'outlook': (briefing or {}).get('outlook', ''),
    }
