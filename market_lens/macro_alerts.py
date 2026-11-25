"""Macroeconomic change digest, diffusion index, and regime-shift alerts."""
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from alpha_engine import detect_regime
from strategy_adapter import load_screened_signals

def macro_diffusion(signals):
    """Share of positive vs negative macro/curve signals across tracked series.

    The UST spread and the monthly OECD curve describe the same thing. When both are present the
    daily UST observation stands for the US curve and the monthly proxy is not counted again.
    """
    curve = signals.get('UST.DAILY.10Y2Y')
    skip = {'FIN.USA.curve'} if curve is not None else set()
    pos = sum(1 for k, v in signals.items()
              if k.startswith('FIN.') and k.endswith('.curve') and k not in skip and _positive(v))
    neg = sum(1 for k, v in signals.items()
              if k.startswith('FIN.') and k.endswith('.curve') and k not in skip and not _positive(v))
    if curve is not None:
        pos += int(_positive(curve))
        neg += int(not _positive(curve))
    total = pos + neg
    return {'positive': pos, 'negative': neg, 'diffusion_pct': round(100.0 * pos / total, 2) if total else None}


def _positive(v):
    try:
        return float(v) > 0
    except (TypeError, ValueError):
        return False

def check_macro_alerts(c, csv_path, alert_log=None, at=None):
    """Evaluate recent macro revisions/withdrawals and regime shifts; optionally append to JSONL."""
    signals = load_screened_signals(csv_path, at=at)
    regime = detect_regime(signals)
    diffusion = macro_diffusion(signals)
    revs = c.execute("select count(*) from event where kind in ('revision','withdrawal')").fetchone()[0]
    alert = {
        'regime': regime['regime'],
        'risk_scale': regime['risk_scale'],
        'diffusion_pct': diffusion['diffusion_pct'],
        'revision_or_withdrawal_events': revs,
        'signals_active': len(signals),
    }
    if alert_log is not None:
        Path(alert_log).parent.mkdir(parents=True, exist_ok=True)
        with Path(alert_log).open('a', encoding='utf-8') as f:
            f.write(json.dumps(alert) + '\n')
    return alert

def detect_macro_chains(signals, inflation_z=None, yield_change=None):
    """Identify economic cause-and-effect chains from screened macro signals."""
    chains = []
    curve = _dec(signals.get('UST.DAILY.10Y2Y'))
    sofr = _dec(signals.get('NYFED.SOFR'))
    diff = macro_diffusion(signals).get('diffusion_pct')
    inf_z = _dec(inflation_z)
    dy = _dec(yield_change)
    # Follow the observed policy-rate environment rather than a hardcoded 4.5%: an inverted curve
    # with any positive policy rate is a tightening-to-slowdown chain.
    if curve is not None and curve < 0 and sofr is not None and sofr > 0:
        chains.append({'chain': 'tightening_to_slowdown', 'signal': f'10Y-2Y={curve}, SOFR={sofr}', 'implication': 'credit tightening -> growth slowdown -> favor TREASURY/TBILL'})
    if curve is not None and curve > Decimal('0.25') and diff is not None and diff >= 60.0:
        chains.append({'chain': 'reflation_expansion', 'signal': f'10Y-2Y={curve}, diffusion={diff}%', 'implication': 'broadening expansion -> favor EQUITY/COMMODITY'})
    if inf_z is not None and inf_z < Decimal('-0.5') and dy is not None and dy < 0:
        chains.append({'chain': 'disinflationary_pivot', 'signal': f'inf_z={inf_z}, dY={dy}', 'implication': 'easing cycle -> favor TREASURY/EQUITY'})
    return chains


def _dec(x):
    try:
        d = Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return d if d.is_finite() else None


def relative_macro_divergence(z_a, z_b, threshold=1.0):
    """Cross-country relative value: spot when one economy's macro Z-score diverges from another's."""
    a, b = _dec(z_a), _dec(z_b)
    if a is None or b is None:
        return {'divergent': False, 'spread_z': None}
    spread = round(float(a - b), 4)
    return {'divergent': abs(spread) >= threshold, 'spread_z': spread, 'leader': 'A' if spread > 0 else 'B'}


def inflation_yield_divergence(inflation_z, yield_change):
    """Flag when inflation is hot (> +0.5z) but long yields are falling (< 0), signaling bond-market slowdown fears."""
    z, dy = _dec(inflation_z), _dec(yield_change)
    if z is None or dy is None:
        return False
    return z > Decimal('0.5') and dy < 0
