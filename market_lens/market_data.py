"""Market data collection: OHLCV (daily and intraday), corporate actions and cross-asset series.

Source: Yahoo Finance's chart endpoint. It is **unofficial** — undocumented and able to change or
rate-limit without notice — so nothing here is presented as a vetted dataset, every payload is
stored raw for re-checking, and a missing dataset is reported as missing rather than skipped.

The split between adjusted and unadjusted prices is deliberate, because the two answer different
questions:

* **Unadjusted** OHLCV is stored. It is what you need for share quantities, dollar volume and
  margin. It is also a fact about a day, so it never changes.
* **Adjusted** prices are derived, never stored as if they were facts: ``close x factor``, where
  the factor is the cumulative split/dividend adjustment *known as of a given time*. A dividend
  changes adjusted history retroactively, so storing "the adjusted close" as a column would
  silently rewrite the past.

Intraday bars are fetched, but each interval has a real history limit at the provider. That limit
is declared in ``INTERVAL_LIMITS`` and reported, so nobody assumes months of 1-minute data exist
when only days do.
"""
import datetime as dt
import json, math
from urllib.parse import quote

from net import fetch, FetchFailure
from sources import guard
import research_store as rs

SOURCE = 'yahoo'
BASE_URL = 'https://query1.finance.yahoo.com/v8/finance/chart/%s'

# ``range=max`` is NOT usable: the endpoint silently returns a thinned series (about 169 points
# spanning four decades) instead of the full history, and withholds the event list entirely. An
# explicit epoch window returns the real series with every dividend and split. Requesting windows
# is therefore the only way to get honest history from this source.
EPOCH_START = 0  # epoch 0: the provider clamps to the instrument's first trade date.

# What the provider actually serves per interval. These are hard limits, not preferences: asking
# for more returns a shorter series, which would be mistaken for a full history if unreported.
INTERVAL_LIMITS = {
    '1m': ('7d', 'about 7 days'),
    '2m': ('60d', 'about 60 days'),
    '5m': ('60d', 'about 60 days'),
    '15m': ('60d', 'about 60 days'),
    '30m': ('60d', 'about 60 days'),
    '60m': ('730d', 'about 2 years'),
    '1d': ('max', 'full history'),
    '1wk': ('max', 'full history'),
}

# Cross-asset and benchmark symbols worth having for regime work. Kept in one place so a coverage
# report can be honest about which ones were actually retrieved.
CROSS_ASSET = {
    '^GSPC': 'S&P 500 index level',
    '^RUT': 'Russell 2000 index level',
    '^VIX': 'CBOE volatility index',
    '^MOVE': 'ICE BofA bond volatility index',
    'DX-Y.NYB': 'US dollar index',
    'EURUSD=X': 'EUR/USD',
    'CL=F': 'WTI crude futures',
    'GC=F': 'Gold futures',
    'HYG': 'High-yield credit ETF',
    'LQD': 'Investment-grade credit ETF',
    'TLT': 'Long Treasury ETF',
}

# Treasury yield tenors available from the FRED feed for the risk-free curve. Yahoo does not serve
# them, so this is a declared pointer rather than a silent gap.
RISK_FREE_NOTE = ('Treasury yield curve is collected from the official US Treasury feed '
                  '(us-treasury-daily) into the main journal, not from this price source.')


class MarketDataError(Exception):
    """A price/action dataset could not be obtained or failed validation."""


def _num(x):
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_chart(symbol, payload, interval='1d'):
    """Parse one chart response into bars, adjusted closes and corporate actions.

    Pure and offline-testable. Rows are validated: a bar without a usable close is dropped, a
    duplicate or out-of-order timestamp is refused, and OHLC consistency is checked rather than
    trusted (a high below the low is a corrupt row, not a trading signal).
    """
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise MarketDataError('%s: response is not JSON' % symbol) from None
    chart = (doc or {}).get('chart') or {}
    if chart.get('error'):
        err = chart['error']
        raise MarketDataError('%s: %s' % (symbol, err.get('description') or err.get('code') or 'rejected'))
    results = chart.get('result') or []
    if not results:
        raise MarketDataError('%s: no data returned' % symbol)
    res = results[0]
    stamps = res.get('timestamp') or []
    ind = res.get('indicators') or {}
    quote = ((ind.get('quote') or [{}])[0] or {})
    adj = ((ind.get('adjclose') or [{}])[0] or {}).get('adjclose')
    opens, highs, lows = quote.get('open'), quote.get('high'), quote.get('low')
    closes, vols = quote.get('close'), quote.get('volume')

    # A thinned series is indistinguishable from real history without a check: ``range=max`` was
    # observed returning 169 points across 1980-2026 with no events, which would silently make a
    # 40-year sample look like a 169-observation one. Expected granularity is one bar per trading
    # day for daily data and per week for weekly; a fraction far below that means the provider
    # degraded the response, so it is refused rather than stored.
    if len(stamps) > 1:
        span_days = (int(stamps[-1]) - int(stamps[0])) / 86400.0
        per_bar_days = {'1d': 1.4, '1wk': 7.0}.get(interval, 0.0)
        if per_bar_days and span_days > 60:
            expected = span_days / per_bar_days
            if len(stamps) < expected * 0.4:
                raise MarketDataError(
                    '%s: provider returned a thinned series (%d bars over %.0f days; about %.0f '
                    'expected); refusing to store it as history'
                    % (symbol, len(stamps), span_days, expected))

    bars, seen = [], set()
    for i, t in enumerate(stamps):
        close = _num(closes[i]) if closes and i < len(closes) else None
        if close is None or close <= 0:
            continue  # a genuine gap is skipped, never interpolated
        ts = int(t)
        if ts in seen:
            raise MarketDataError('%s: duplicate timestamp %d' % (symbol, ts))
        if bars and ts <= bars[-1]['ts']:
            raise MarketDataError('%s: timestamps are not increasing' % symbol)
        seen.add(ts)
        o = _num(opens[i]) if opens and i < len(opens) else None
        h = _num(highs[i]) if highs and i < len(highs) else None
        lo = _num(lows[i]) if lows and i < len(lows) else None
        # Drop an inconsistent OHLC set to the close rather than storing an impossible bar.
        if h is not None and lo is not None and h < lo:
            h = lo = None
        bars.append({'ts': ts, 'open': o, 'high': h, 'low': lo, 'close': close,
                     'volume': _num(vols[i]) if vols and i < len(vols) else None})
    if not bars:
        raise MarketDataError('%s: no usable bars' % symbol)

    # Cumulative adjustment factor, relative to the latest bar (which is 1.0 by construction).
    factors = []
    if adj and len(adj) == len(closes):
        last = None
        for i, a in enumerate(adj):
            if i >= len(stamps):
                break
            av = _num(a)
            cv = _num(closes[i]) if closes else None
            if av is None or cv is None or cv <= 0:
                continue
            f = av / cv
            if not math.isfinite(f) or f <= 0:
                continue
            last = f
            factors.append((int(stamps[i]), f))

    events = res.get('events') or {}
    actions = []
    for key, kind in (('dividends', 'dividend'), ('splits', 'split')):
        for _, ev in (events.get(key) or {}).items():
            try:
                ex = int(ev.get('date'))
            except (TypeError, ValueError):
                continue
            if kind == 'dividend':
                amt = _num(ev.get('amount'))
                if amt is None or amt < 0:
                    continue
                actions.append({'kind': 'dividend', 'ex_ts': ex, 'amount': amt, 'ratio': None})
            else:
                num, _, den = str(ev.get('splitRatio') or '').partition(':')
                a, b = _num(num), _num(den)
                if not a or not b or a <= 0 or b <= 0:
                    continue
                actions.append({'kind': 'split', 'ex_ts': ex, 'ratio': a / b, 'amount': None})
    actions.sort(key=lambda x: (x['ex_ts'], x['kind']))

    meta = res.get('meta') or {}
    return {
        'symbol': str(meta.get('symbol') or symbol).upper(),
        'interval': interval,
        'bars': bars,
        'adjustment_factors': factors,
        'actions': actions,
        'currency': meta.get('currency'),
        'exchange': meta.get('exchangeName'),
        'first_utc': bars[0]['ts'],
        'last_utc': bars[-1]['ts'],
    }


def _resolve_range(interval, requested):
    """Resolve a requested window into the query form this endpoint actually honours.

    Two provider quirks make this necessary, and both were measured rather than assumed:

    * ``range=max`` does **not** return full history. It returns a thinned series (about 169
      points spanning 1980-2026) and omits the event list, which would silently turn a 40-year
      backtest into a 169-observation one. Full history is therefore requested with an explicit
      epoch window (``period1=0``), which returns the genuine ~11,600 daily bars with every
      dividend and split.
    * Epoch windows are rejected (HTTP 422) for some sub-daily intervals where ``range=`` works.

    So: full history uses an epoch window, and a bounded window uses ``range=``. Either way the
    parsed series is checked for thinning, so a degraded response cannot be stored as history.

    Returns ``(query_fragment, label)``.
    """
    import re
    limit, _ = INTERVAL_LIMITS.get(interval, ('max', 'full history'))
    rng = (requested or ('max' if limit == 'max' else limit)).strip().lower()
    if rng in ('max', 'all', ''):
        if limit == 'max':
            return 'period1=%d&period2=%d' % (EPOCH_START, int(__import__('time').time())), 'max'
        rng = limit
    m = re.fullmatch(r'(\d+)([dwmy])', rng)
    if not m:
        raise MarketDataError('unrecognised range %r (use e.g. 5y, 60d, max)' % (requested,))
    n, unit = int(m.group(1)), m.group(2)
    days = n * {'d': 1, 'w': 7, 'm': 31, 'y': 366}[unit]
    if days <= 0:
        raise MarketDataError('range must be positive')
    if limit != 'max':
        # Clip to the interval's real history so a request cannot appear to be satisfied.
        cap = int(re.fullmatch(r'(\d+)d', limit).group(1))
        if days > cap:
            rng = '%dd' % cap
    return 'range=%s' % rng, rng


def fetch_bars(symbol, interval='1d', rng=None):
    """Fetch one symbol's bars. Raises ``MarketDataError`` with a reason on any problem."""
    sym = (symbol or '').strip().upper()
    if not sym:
        raise MarketDataError('empty symbol')
    guard(all(ch.isalnum() or ch in '._-^=' for ch in sym), 'invalid ticker: ' + sym)
    if interval not in INTERVAL_LIMITS:
        raise MarketDataError('unsupported interval %r (choose %s)'
                              % (interval, ', '.join(sorted(INTERVAL_LIMITS))))
    window, window_label = _resolve_range(interval, rng)
    url = BASE_URL % quote(sym, safe='') + '?' + window + '&interval=' + quote(interval) + '&events=div%2Csplits'
    try:
        body, final_url, _, _ = fetch(url)
    except FetchFailure as e:
        raise MarketDataError(str(e)) from None
    if 'query1.finance.yahoo.com' not in final_url:
        raise MarketDataError('%s: unexpected host in response' % sym)
    parsed = parse_chart(sym, body, interval)
    parsed['window_requested'] = window_label
    parsed['window_limit'] = INTERVAL_LIMITS[interval][1]
    parsed['query'] = window
    parsed['raw'] = body
    parsed['url'] = url
    return parsed


def ingest_symbol(c, symbol, interval='1d', rng=None, fetched_utc=None):
    """Fetch one symbol and write it to the research store. Returns a per-symbol report."""
    from store import now as _now
    fetched = fetched_utc or _now()
    parsed = fetch_bars(symbol, interval, rng)
    sym = parsed['symbol']
    raw_hash = rs.put_raw(c, SOURCE, parsed['url'], parsed['raw'], fetched)
    conflicts = rs.bar_conflicts(c, sym, interval, parsed['bars'])
    new_bars = rs.put_bars(c, sym, interval, parsed['bars'], SOURCE, fetched)
    new_factors = rs.put_adj_factors(c, sym, parsed['adjustment_factors'], fetched, SOURCE) \
        if parsed['adjustment_factors'] else 0
    new_actions = rs.put_actions(c, sym, parsed['actions'], SOURCE, fetched) if parsed['actions'] else 0
    return {
        'symbol': sym,
        'interval': interval,
        'bars': len(parsed['bars']),
        'new_bars': new_bars,
        'adjustment_factors': new_factors,
        'actions': len(parsed['actions']),
        'new_actions': new_actions,
        'first_utc': parsed['first_utc'],
        'last_utc': parsed['last_utc'],
        'window': parsed['window_requested'],
        'history_limit': parsed['window_limit'],
        'currency': parsed['currency'],
        'raw_sha256': raw_hash,
        # A provider price correction is reported, never applied over the stored bar.
        'conflicts': conflicts[:5],
        'conflict_count': len(conflicts),
    }


def collect(c, symbols, intervals=('1d',), rng=None, daily_limit=None):
    """Collect several symbols across one or more intervals, tolerating individual failures."""
    report = {'source': SOURCE, 'unofficial': True, 'ok': [], 'failed': {},
              'intervals': list(intervals), 'conflicts': 0}
    for interval in intervals:
        for sym in symbols:
            try:
                r = ingest_symbol(c, sym, interval, rng=rng)
            except MarketDataError as e:
                report['failed']['%s@%s' % (sym, interval)] = str(e)
                continue
            report['ok'].append(r)
            report['conflicts'] += r['conflict_count']
            if daily_limit and interval == '1d':
                break
    return report


def to_ohlcv_rows(bars):
    """Plain rows for CSV/JSON output."""
    return [{'ts': b['ts'],
             'utc': dt.datetime.fromtimestamp(b['ts'], dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
             'open': b.get('open'), 'high': b.get('high'), 'low': b.get('low'),
             'close': b['close'], 'volume': b.get('volume')} for b in bars]


def describe(report):
    if not report:
        return 'no market data report'
    n = len(report.get('ok') or [])
    parts = ['%d symbol/interval pairs stored' % n]
    if report.get('failed'):
        parts.append('failed: ' + ', '.join(list(report['failed'])[:6]))
    if report.get('conflicts'):
        parts.append('%d stored bars differ from a new download (kept, not overwritten)'
                     % report['conflicts'])
    return 'Yahoo Finance (unofficial) - ' + '; '.join(parts)
