"""One source of truth for asset classes, correlation priors and the quadrant allocation.

Before this module the codebase had four different quadrant -> allocation tables
(``ALL_WEATHER_MAP``, ``dalio_quadrant_weights``, ``SECTOR_MAP`` and the operation ladder) that
disagreed with each other, and sizing assumed zero correlation between every pair of assets. Both
are now derived from here.
"""
import math

# Ticker -> fine-grained class used by the correlation priors.
TICKER_CLASS = {
    # cash / bills
    'BIL': 'cash', 'SGOV': 'cash', 'SHV': 'cash', 'TBILL': 'cash', 'CASH': 'cash',
    # short / intermediate / long Treasuries
    'SHY': 'short_bond', 'VGSH': 'short_bond', 'SCHO': 'short_bond',
    'IEF': 'bond', 'GOVT': 'bond', 'VGIT': 'bond', 'AGG': 'bond', 'BND': 'bond', 'TREASURY': 'bond',
    'TLT': 'long_bond', 'EDV': 'long_bond', 'VGLT': 'long_bond', 'ZROZ': 'long_bond',
    # credit
    'LQD': 'credit', 'HYG': 'credit', 'JNK': 'credit', 'VCIT': 'credit',
    # equities
    'SPY': 'equity', 'VOO': 'equity', 'IVV': 'equity', 'VTI': 'equity', 'IWM': 'equity',
    'DIA': 'equity', 'EQUITY': 'equity', 'XLV': 'equity', 'XLP': 'equity', 'XLU': 'equity',
    'XLI': 'equity', 'XLB': 'equity', 'XLE': 'equity', 'XLF': 'equity', 'XLY': 'equity',
    'QQQ': 'growth', 'XLK': 'growth', 'VUG': 'growth',
    'VEA': 'intl_equity', 'EFA': 'intl_equity', 'VXUS': 'intl_equity', 'EEM': 'intl_equity',
    'VWO': 'intl_equity', 'IEFA': 'intl_equity',
    # real assets
    'GLD': 'gold', 'IAU': 'gold', 'GLDM': 'gold', 'GOLD': 'gold',
    'DBC': 'commodity', 'GSG': 'commodity', 'PDBC': 'commodity', 'COMMODITY': 'commodity',
}

# Fine class -> All-Weather bucket used by the quadrant table.
CLASS_BUCKET = {
    'cash': 'TBILL', 'short_bond': 'TREASURY', 'bond': 'TREASURY', 'long_bond': 'TREASURY',
    'credit': 'TREASURY', 'equity': 'EQUITY', 'growth': 'EQUITY', 'intl_equity': 'EQUITY',
    'gold': 'GOLD', 'commodity': 'COMMODITY',
}

# Cross-asset-class correlation priors. Symmetric; unlisted pairs fall back to the default.
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

# Two *different* tickers in the same class (SPY and VOO) are close substitutes, not identical.
SAME_CLASS_CORR = 0.95

# Correlation assumed when a ticker is not recognised. Zero would understate the risk of a
# correlated book (an equity-only book sized at "10%" actually ran ~16%), so the unknown case is
# deliberately conservative.
UNKNOWN_CORR = 0.50

# The one quadrant -> All-Weather allocation table. Everything else derives from it.
ALL_WEATHER_MAP = {
    'rising_growth_falling_inflation': {'EQUITY': 0.60, 'TREASURY': 0.25, 'GOLD': 0.10, 'TBILL': 0.05},
    'rising_growth_rising_inflation':  {'EQUITY': 0.35, 'GOLD': 0.30, 'COMMODITY': 0.25, 'TBILL': 0.10},
    'falling_growth_rising_inflation': {'GOLD': 0.35, 'COMMODITY': 0.25, 'TBILL': 0.30, 'TREASURY': 0.10},
    'falling_growth_falling_inflation': {'TREASURY': 0.50, 'GOLD': 0.25, 'TBILL': 0.25},
    'unknown':                         {'TBILL': 1.00},
}


def asset_class(ticker):
    """Fine class of a ticker, or ``None`` if unknown."""
    return TICKER_CLASS.get(str(ticker or '').upper())


def bucket(ticker):
    """All-Weather bucket (EQUITY/TREASURY/GOLD/COMMODITY/TBILL) of a ticker, or ``None``."""
    cls = asset_class(ticker)
    return CLASS_BUCKET.get(cls) if cls else None


def class_corr(a, b, default=0.25):
    """Correlation prior between two *classes*."""
    if a == b:
        return 1.0
    return CLASS_CORR.get((a, b), CLASS_CORR.get((b, a), default))


def pair_corr(a, b, default=UNKNOWN_CORR):
    """Correlation prior between two *tickers*."""
    if a == b:
        return 1.0
    ca, cb = asset_class(a), asset_class(b)
    if ca is None or cb is None:
        return default
    if ca == cb:
        return SAME_CLASS_CORR
    return class_corr(ca, cb, default)


def portfolio_vol(weights, vols, corr=None):
    """Portfolio volatility with correlations.

    ``corr`` may be ``None`` (class priors, conservative default for unknown tickers), a single
    number used for every pair, or a callable ``corr(a, b)``.
    """
    names = [a for a, w in (weights or {}).items() if _finite(w) and _finite(vols.get(a))]
    var = 0.0
    for a in names:
        for b in names:
            if a == b:
                rho = 1.0
            elif callable(corr):
                rho = corr(a, b)
            elif corr is not None:
                rho = max(-1.0, min(1.0, float(corr)))
            else:
                rho = pair_corr(a, b)
            var += float(weights[a]) * float(weights[b]) * float(vols[a]) * float(vols[b]) * rho
    return math.sqrt(max(0.0, var))


def quadrant_tilts(assets, quadrant, strength=0.5):
    """Multiplicative tilts that lean an allocation toward the quadrant's favoured buckets.

    For each recognised asset the tilt is ``(1 - s) + s * q[bucket] / mean_q`` where ``q`` is the
    quadrant's weight for the asset's bucket and ``mean_q`` is the mean over the buckets actually
    present. ``s = 0`` leaves weights unchanged; ``s = 1`` follows the quadrant table fully. An
    unknown quadrant or an unrecognised asset gets 1.0 (no view, no tilt).
    """
    try:
        s = max(0.0, min(1.0, float(strength)))
    except (TypeError, ValueError):
        s = 0.0
    out = {a: 1.0 for a in assets}
    table = ALL_WEATHER_MAP.get(quadrant)
    if not table or quadrant == 'unknown' or s == 0.0:
        return out
    buckets = {a: bucket(a) for a in assets}
    present = {b for b in buckets.values() if b}
    if not present:
        return out
    mean_q = sum(table.get(b, 0.0) for b in present) / len(present)
    if mean_q <= 0:
        return out
    for a, b in buckets.items():
        if b:
            out[a] = (1.0 - s) + s * table.get(b, 0.0) / mean_q
    return out


def _finite(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v)


def blended_vol(prices, short_window=20, long_window=250, halflife=30, periods_per_year=252):
    """Annualised volatility that is both stable and quick to react to a shock.

    Returns ``max(short-window stdev, EWMA vol over up to long_window returns)`` from prices
    (oldest first). A 20-day estimate alone is noisy and drives needless rebalancing; a long
    estimate alone is slow to see a crash. Taking the larger is the conservative blend: it sizes
    down quickly when risk rises and does not size up on a short calm patch. Returns ``None`` when
    fewer than two log returns are available or the series has no variance.
    """
    clean = []
    for p in prices or []:
        try:
            v = float(p)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v > 0:
            clean.append(v)
    rets = [math.log(b / a) for a, b in zip(clean, clean[1:])]
    if len(rets) < 2:
        return None
    short = rets[-int(short_window):]
    mean_s = sum(short) / len(short)
    sd_short = math.sqrt(sum((r - mean_s) ** 2 for r in short) / max(1, len(short) - 1))
    long = rets[-int(long_window):]
    lam = 0.5 ** (1.0 / max(1.0, float(halflife)))
    var, wsum = 0.0, 0.0
    weight = 1.0
    for r in reversed(long):  # newest gets weight 1, older decays
        var += weight * r * r
        wsum += weight
        weight *= lam
    sd_ewma = math.sqrt(var / wsum) if wsum > 0 else 0.0
    sd = max(sd_short, sd_ewma)
    if not math.isfinite(sd) or sd <= 1e-12:
        return None
    return sd * math.sqrt(periods_per_year)
