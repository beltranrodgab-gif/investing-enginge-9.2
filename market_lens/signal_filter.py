"""Institutional 9-rule signal filter (Dalio macro logic + Simons statistical discipline).

Every rule named in the docstring is reachable from ``filter_signals`` or a documented public
function. Dedupe is deterministic and independent of CSV row order: the freshest, cleanest, most
granular observation represents its group, never simply the first row encountered.
"""
import calendar
import math
import re
import statistics
from decimal import Decimal, InvalidOperation
from reliability import read_export, FALLBACKS
from sources import M

FREQ_RANK = {'D': 0, 'M': 1, 'W': 1, 'Q': 2, 'A': 3}  # lower is fresher/more granular

# Publisher period formats seen in the exports. They must be normalised before comparison:
# as raw strings '2026Q3' sorts *after* '2026-11-18' and an unpadded '2026-9-1' sorts after
# '2026-11-18' too, so a stale quarterly row could beat a fresh daily one on recency alone.
_PERIOD_FORMS = (
    (re.compile(r'^(\d{4})[-_ ]?[Qq]([1-4])$'), lambda m: (int(m.group(1)), int(m.group(2)) * 3)),
    (re.compile(r'^(\d{4})[-_ ]?[Hh]([12])$'), lambda m: (int(m.group(1)), int(m.group(2)) * 6)),
    (re.compile(r'^(\d{4})$'), lambda m: (int(m.group(1)), 12)),
    (re.compile(r'^(\d{4})[-/.](\d{1,2})$'), lambda m: (int(m.group(1)), int(m.group(2)))),
    (re.compile(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})'),
     lambda m: (int(m.group(1)), int(m.group(2)), int(m.group(3)))),
)


def period_end(text):
    """Normalise a publisher period to an ISO *end* date, so periods of different formats compare.

    A month or a quarter is dated to its last day, because that is when the observation covers up
    to. Anything unrecognised becomes the empty string, which sorts before every real date, so an
    unreadable period can never win a recency comparison.
    """
    raw = str(text or '').strip()
    if not raw:
        return ''
    for pattern, parts in _PERIOD_FORMS:
        m = pattern.match(raw)
        if not m:
            continue
        got = parts(m)
        try:
            if len(got) == 2:
                year, month = got
                if not 1 <= month <= 12:
                    return ''
                return '%04d-%02d-%02d' % (year, month, calendar.monthrange(year, month)[1])
            year, month, day = got
            return '%04d-%02d-%02d' % (year, month, day)
        except (ValueError, calendar.IllegalMonthError):
            return ''
    return ''


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def winsorize_zscore(values, cap=3.5):
    """Rule 5 & 6: Compute stationary Z-score of latest change and winsorize at +-cap."""
    clean = [v for v in (_num(x) for x in (values or [])) if v is not None]
    if len(clean) < 4:
        return None
    diffs = [b - a for a, b in zip(clean, clean[1:])]
    hist = diffs[:-1] if len(diffs) > 2 else diffs
    mu = statistics.mean(hist)
    sd = statistics.stdev(hist) if len(hist) >= 2 else 0.0
    if sd <= 1e-12:
        return 0.0
    z = (diffs[-1] - mu) / sd
    return max(-cap, min(cap, z))


def history_zscore(values, window=60, cap=3.5):
    """Level z-score of the newest value against its trailing ``window`` history.

    This is the growth/inflation z-score the quadrant and regime logic consume. It never looks
    forward: only the value's own past is used.
    """
    clean = [v for v in (_num(x) for x in (values or [])) if v is not None]
    if len(clean) < 4:
        return None
    tail = clean[-(max(4, int(window)) + 1):]
    hist, latest = tail[:-1], tail[-1]
    if len(hist) < 3:
        return None
    mu = statistics.mean(hist)
    sd = statistics.stdev(hist)
    # A (near-)constant history: float noise of 1e-15 must not be read as a +/-cap surprise.
    tol = 1e-9 * max(1.0, abs(mu))
    if sd <= tol:
        diff = latest - mu
        return 0.0 if abs(diff) <= tol else (cap if diff > 0 else -cap)
    return max(-cap, min(cap, (latest - mu) / sd))


def pearson_corr(xs, ys):
    """Compute Pearson correlation on equal-length aligned lists."""
    a = [v for v in (_num(x) for x in (xs or [])) if v is not None]
    b = [v for v in (_num(y) for y in (ys or [])) if v is not None]
    n = min(len(a), len(b))
    if n < 4:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = statistics.mean(a), statistics.mean(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den > 1e-12 else 0.0


def _deltas(values):
    return [b - a for a, b in zip(values, values[1:])]


def _aligned(a, b):
    """Return the differenced overlap of two level series, or (None, None) if too short."""
    n = min(len(a), len(b))
    if n < 3:
        return None, None
    return _deltas(a[-n:]), _deltas(b[-n:])


def prune_correlated(series_histories, threshold=0.85, min_overlap=8):
    """Rule 7: drop series that duplicate another series' *movements*.

    Correlation is computed between first differences over the common overlap, not between levels.
    Two country curves that both trend down in levels can correlate at 0.9+ with unrelated
    month-to-month direction, so pruning on levels deleted independent country signals.
    """
    kept = {}
    for sid, hist in series_histories.items():
        clean = [v for v in (_num(x) for x in (hist or [])) if v is not None]
        if len(clean) < 4 or statistics.pstdev(clean) <= 1e-12:
            continue  # drop dead/flat series instead of electing them as representatives
        duplicate = False
        for other in kept.values():
            if min(len(clean), len(other)) < min_overlap:
                continue
            da, db = _aligned(clean, other)
            if da is None or statistics.pstdev(da) <= 1e-12 or statistics.pstdev(db) <= 1e-12:
                continue
            if abs(pearson_corr(da, db)) > threshold:
                duplicate = True
                break
        if not duplicate:
            kept[sid] = clean
    return list(kept.keys())


def _representative_key(row, meta):
    """Rank key where a *larger* tuple means "better representative".

    Ordering is freshest period first, then most granular frequency, then clean over flagged,
    then series id as a tie-break. Because it is a total order the winner never depends on the row
    order in the CSV.

    The frequency term is negated on purpose: ``FREQ_RANK`` is 0 for daily and 3 for annual, so
    under a largest-wins comparison ``-rank`` prefers daily. The earlier code kept the rank
    positive under a smallest-wins comparison, which inverted the preference and let an annual
    series represent a group that also had a daily series.
    """
    return (
        period_end(row.get('period')),
        -FREQ_RANK.get(meta.get('freq', 'M'), 9),
        0 if str(row.get('quality') or '') == 'unflagged / normal' else -1,
        str(row.get('series') or ''),
    )


def filter_signals(csv_path, freq=None, dedupe_groups=True, at=None):
    """Rules 1, 2, 4, 8: Fail-closed ingestion, frequency split, group deduplication, point-in-time order.

    Dedupe keeps exactly one row per ``signal_group``, chosen by recency/quality/granularity rather
    than file order (row order previously decided whether the live regime saw the daily UST curve or
    the monthly OECD curve).
    """
    rows = read_export(csv_path, at=at)
    candidates = {}
    for r in rows:
        sid = r['series']
        meta = M.get(sid, {})
        if freq and meta.get('freq') != freq:
            continue
        try:
            value = Decimal(r['screened_value'])
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not value.is_finite():
            continue
        grp = r.get('signal_group') or sid
        item = {
            'sid': sid,
            'key': _representative_key(r, meta),
            'entry': {
                'value': value,
                'freq': meta.get('freq', ''),
                'group': grp,
                'indexed_utc': r.get('snapshot_indexed_utc'),
                'usable_until_utc': r.get('usable_until_utc'),
                'period': r.get('period'),
                'quality': r.get('quality'),
            },
        }
        if dedupe_groups:
            cur = candidates.get(grp)
            if cur is None or item['key'] > cur['key']:
                candidates[grp] = item
        else:
            candidates[sid] = item
    out = {v['sid']: v['entry'] for v in candidates.values()}
    for primary, backup in FALLBACKS.items():
        if primary not in out and backup in out:
            out[primary] = dict(out[backup], is_fallback=True, fallback_from=backup)
    return out


def group_representatives(csv_path, at=None):
    """Diagnostic: {group: chosen_series} plus the candidates that lost, for auditing a decision."""
    rows = read_export(csv_path, at=at)
    groups = {}
    for r in rows:
        meta = M.get(r['series'], {})
        groups.setdefault(r.get('signal_group') or r['series'], []).append((_representative_key(r, meta), r['series']))
    out = {}
    for g, v in groups.items():
        best = max(v)[1]
        out[g] = {'chosen': best, 'rejected': sorted(x[1] for x in v if x[1] != best)}
    return out


def classify_macro_quadrant(growth_z, inflation_z):
    """Rule 8 (Dalio): Map stationary growth & inflation Z-scores into 2x2 All-Weather quadrants."""
    g, i = _num(growth_z), _num(inflation_z)
    if g is None or i is None:
        return 'unknown'
    if g >= 0 and i < 0:
        return 'rising_growth_falling_inflation'
    if g >= 0 and i >= 0:
        return 'rising_growth_rising_inflation'
    if g < 0 and i >= 0:
        return 'falling_growth_rising_inflation'
    return 'falling_growth_falling_inflation'


def revision_noise_ok(c, series_id, max_revisions=6, at=None):
    """Rule 9 (Simons): Reject series with excessive revision/withdrawal instability.

    With ``at`` only revisions recorded by polls started at or before ``at`` count, so a
    historical decision is not influenced by revisions that happened later.
    """
    try:
        if at is None:
            revs = c.execute("select count(*) from event where series=? and kind in ('revision','withdrawal')",
                             (series_id,)).fetchone()[0]
        else:
            from store import iso
            stamp = at if isinstance(at, str) else iso(at)
            revs = c.execute("select count(*) from event join poll on poll.id=event.id where event.series=? "
                             "and event.kind in ('revision','withdrawal') and poll.started<=?",
                             (series_id, stamp)).fetchone()[0]
    except Exception:
        return False
    return revs <= max_revisions


def granger_causality_test(y_series, x_series, f_threshold=4.0):
    """Lag-1 Granger Causality F-test: tests if X_{t-1} predicts Y_t beyond Y_{t-1} alone."""
    y = [v for v in (_num(x) for x in (y_series or [])) if v is not None]
    x = [v for v in (_num(x) for x in (x_series or [])) if v is not None]
    n = min(len(y), len(x))
    if n < 10:
        return {'causal': False, 'f_stat': 0.0, 'reason': 'insufficient observations'}
    y = y[-n:]
    x = x[-n:]
    yt, yl, xl = y[1:], y[:-1], x[:-1]
    m = len(yt)
    my, ml = statistics.mean(yt), statistics.mean(yl)
    denom_r = sum((a - ml) ** 2 for a in yl)
    b1_r = sum((a - ml) * (b - my) for a, b in zip(yl, yt)) / denom_r if denom_r > 1e-12 else 0.0
    b0_r = my - b1_r * ml
    rss_r = sum((b - (b0_r + b1_r * a)) ** 2 for a, b in zip(yl, yt))

    sx1, sx2 = sum(yl), sum(xl)
    sy = sum(yt)
    s11 = sum(a * a for a in yl)
    s22 = sum(a * a for a in xl)
    s12 = sum(a * b for a, b in zip(yl, xl))
    s1y = sum(a * b for a, b in zip(yl, yt))
    s2y = sum(a * b for a, b in zip(xl, yt))
    A = [[m, sx1, sx2, sy], [sx1, s11, s12, s1y], [sx2, s12, s22, s2y]]
    for i in range(3):
        piv = A[i][i]
        if abs(piv) < 1e-12:
            return {'causal': False, 'f_stat': 0.0, 'reason': 'collinear'}
        for j in range(i, 4):
            A[i][j] /= piv
        for k in range(3):
            if k != i:
                fac = A[k][i]
                for j in range(i, 4):
                    A[k][j] -= fac * A[i][j]
    c0, c1, c2 = A[0][3], A[1][3], A[2][3]
    rss_u = sum((b - (c0 + c1 * a1 + c2 * a2)) ** 2 for a1, a2, b in zip(yl, xl, yt))
    if rss_u <= 1e-12:
        return {'causal': rss_r > 1e-9, 'f_stat': 999.0 if rss_r > 1e-9 else 0.0}
    if m - 3 <= 0:
        return {'causal': False, 'f_stat': 0.0, 'reason': 'insufficient degrees of freedom'}
    f_stat = ((rss_r - rss_u) / 1.0) / (rss_u / (m - 3))
    return {'causal': f_stat >= f_threshold, 'f_stat': round(f_stat, 4)}


def hurst_exponent(series, max_lag=None):
    """Estimate the Hurst exponent of the *differenced* series with multi-window R/S scaling.

    R/S analysis is only meaningful on a stationary series. Applied to an integrated level series
    (a price path) both the range and the standard deviation grow with the window, so the ratio is
    flat and H collapses toward 0 or saturates at 1; that is why the previous single-window version
    reported pure random walks as H ~= 0.83. Here the input levels are differenced first, then
    log(R/S) is regressed on log(window) across many window sizes:

    * white noise (random walk) -> H ~= 0.5
    * persistent/momentum returns -> H > 0.5
    * mean-reverting returns -> H < 0.5
    """
    clean = [v for v in (_num(x) for x in (series or [])) if v is not None]
    if len(clean) < 24:
        return {'hurst': None, 'regime': 'insufficient_data'}
    deltas = [b - a for a, b in zip(clean, clean[1:])]
    n = len(deltas)
    if n < 20 or statistics.pstdev(deltas) <= 1e-12:
        return {'hurst': None, 'regime': 'insufficient_data'}
    max_lag = int(max_lag or max(8, n // 2))
    sizes = []
    size = 8
    while size <= max_lag:
        sizes.append(size)
        size = int(size * 1.6) + 1
    if len(sizes) < 3:
        return {'hurst': None, 'regime': 'insufficient_data'}
    xs, ys = [], []
    for w in sizes:
        chunks = n // w
        if chunks < 2:
            continue
        vals = []
        for c in range(chunks):
            seg = deltas[c * w:(c + 1) * w]
            mean = sum(seg) / len(seg)
            cum, s = 0.0, []
            for v in seg:
                cum += v - mean
                s.append(cum)
            r = max(s) - min(s)
            sd = statistics.pstdev(seg)
            if r > 1e-12 and sd > 1e-12:
                vals.append(r / sd)
        if len(vals) >= 2:
            xs.append(math.log(w))
            ys.append(math.log(statistics.mean(vals)))
    if len(xs) < 3:
        return {'hurst': None, 'regime': 'insufficient_data'}
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den <= 1e-12:
        return {'hurst': None, 'regime': 'insufficient_data'}
    h = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    h = max(0.0, min(1.0, h))
    regime = 'trending' if h > 0.55 else 'mean_reverting' if h < 0.45 else 'random_walk'
    return {'hurst': round(h, 4), 'regime': regime, 'windows': len(xs), 'basis': 'first differences'}


FREQ_WINDOW = {'D': 250, 'M': 60, 'Q': 20, 'A': 10}

# Direction of each factor relative to the bucket it feeds. Unemployment *falls* when growth is
# strong, so its z-score must be inverted before it is averaged into growth. Without this a spike
# in unemployment scored as the strongest possible growth reading (+3.5).
FACTOR_SIGN = {'ACT-01': 1.0, 'LAB-02': -1.0, 'YLD-03': 1.0,
               'INF-01': 1.0, 'INF-03': 1.0, 'COM-02': 1.0}
GROWTH_FACTORS = ('ACT-01', 'LAB-02', 'YLD-03')
INFLATION_FACTORS = ('INF-01', 'INF-03', 'COM-02')

# Observations per year, used to turn an index level into a year-on-year rate.
PERIODS_PER_YEAR = {'M': 12, 'Q': 4, 'A': 1, 'W': 52}

# Euro-area members are collapsed into one region: five near-identical votes for the euro area
# against one for the US was a bias, not diversification.
EURO_MEMBERS = {'DE', 'FR', 'IT', 'ES', 'NL', 'BE', 'AT', 'PT', 'IE', 'FI', 'GR', 'EA'}

# Approximate shares of world GDP (nominal, rounded). Only relative size matters: the weights are
# re-normalised over the regions actually present. Unknown regions get a small weight.
REGION_WEIGHT = {'US': 0.26, 'EA': 0.15, 'CN': 0.17, 'JP': 0.04, 'GB': 0.03, 'CA': 0.02,
                 'KR': 0.017, 'AU': 0.017, 'MX': 0.015, 'BR': 0.02, 'CH': 0.008,
                 'SE': 0.006, 'NO': 0.005}
UNKNOWN_REGION_WEIGHT = 0.005


def is_index_level(meta):
    """True when a series is a price-index *level* (e.g. 'index, Jan 2020=100'), not a rate."""
    unit = str((meta or {}).get('unit') or '').lower()
    return unit.startswith('index')


def to_yoy(values, freq):
    """Convert an index-level history into year-on-year % changes.

    A rising price level z-scored against its own past looks 'hot' forever; inflation is the
    *rate* of change. Returns an empty list when the frequency has no annual lag or the history is
    shorter than one year.
    """
    lag = PERIODS_PER_YEAR.get(freq)
    clean = [v for v in (_num(x) for x in (values or [])) if v is not None]
    if not lag or len(clean) <= lag:
        return []
    return [100.0 * (b / a - 1.0) for a, b in zip(clean, clean[lag:]) if a > 0]


def region_of(meta):
    geos = (meta or {}).get('geos') or []
    geo = str(geos[0]).upper() if geos else ''
    return 'EA' if geo in EURO_MEMBERS else (geo or '??')


def _region_weighted(items):
    """Average z-scores within each region, then GDP-weight the regions."""
    by_region = {}
    for sid, region, z in items:
        by_region.setdefault(region, []).append(z)
    if not by_region:
        return None, {}
    raw = {r: REGION_WEIGHT.get(r, UNKNOWN_REGION_WEIGHT) for r in by_region}
    total = sum(raw.values())
    weights = {r: w / total for r, w in raw.items()}
    value = sum(weights[r] * statistics.mean(zs) for r, zs in by_region.items())
    return value, {r: round(w, 4) for r, w in weights.items()}


def macro_z_scores(series_map, window=None, cap=3.5, freq_of=None):
    """Build growth and inflation z-scores from a {series_id: history} map.

    Each series' newest observation is z-scored against its own trailing window (roughly five
    years for every frequency: D 250, M 60, Q 20 bars). Three corrections apply before averaging:

    * **Sign.** Unemployment is inverted (``FACTOR_SIGN``): rising unemployment lowers growth.
    * **Rates, not levels.** A series whose unit is an index level is converted to year-on-year
      % change first, so a steadily rising price level is not read as accelerating inflation.
    * **Region weighting.** Series are averaged within a region (euro-area members collapse into
      ``EA``), and regions are weighted by approximate GDP, so five euro-area series no longer
      outvote the US five to one.
    """
    growth, inflation = [], []
    transformed = []
    for sid, hist in (series_map or {}).items():
        meta = M.get(sid, {})
        factor = meta.get('factor')
        if factor not in FACTOR_SIGN:
            continue
        freq = (freq_of or {}).get(sid) or meta.get('freq') or 'M'
        values = hist
        if is_index_level(meta):
            values = to_yoy(hist, freq)
            transformed.append(sid)
        w = window or FREQ_WINDOW.get(freq, 60)
        z = history_zscore(values, window=w, cap=cap)
        if z is None:
            continue
        z = FACTOR_SIGN[factor] * z
        item = (sid, region_of(meta), z)
        if factor in GROWTH_FACTORS:
            growth.append(item)
        else:
            inflation.append(item)
    g_val, g_weights = _region_weighted(growth)
    i_val, i_weights = _region_weighted(inflation)
    return {
        'growth_z': round(g_val, 4) if g_val is not None else None,
        'inflation_z': round(i_val, 4) if i_val is not None else None,
        'growth_sources': len(growth),
        'inflation_sources': len(inflation),
        'growth_detail': {sid: round(z, 4) for sid, _, z in growth},
        'inflation_detail': {sid: round(z, 4) for sid, _, z in inflation},
        'growth_region_weights': g_weights,
        'inflation_region_weights': i_weights,
        'converted_to_yoy': sorted(transformed),
    }
