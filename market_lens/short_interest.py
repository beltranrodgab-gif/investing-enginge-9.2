"""Short interest from FINRA's public consolidated short-interest endpoint.

**Read this before using the numbers.** The endpoint answers with its *oldest* page and ignores
every narrowing parameter it advertises (``sort``, ``sortFields``, ``compareFilters``,
``domainFilters``, ``dateRangeFilters`` all return the same first page, and several return HTTP
400). It is currently serving settlement dates from 2020. There is therefore no way to ask this
API for the current cycle.

So this module does three things and no more:

* It fetches and stores what the endpoint returns. Those rows are real, dated observations and are
  useful as history, so they are kept with their settlement date intact.
* It checks freshness against the twice-monthly publication schedule and sets
  ``current: False`` with a reason when the newest settlement is older than expected. A stale
  figure is never presented as the current short position.
* It reports borrow fees as **unavailable**. FINRA does not publish stock-loan rates, and short
  interest is not a substitute: a heavily shorted stock can be cheap to borrow while a small short
  position in a hard-to-borrow name is expensive.

Sentinel values are converted rather than stored: FINRA writes ``999.99`` days-to-cover when it
has no usable average daily volume, and that is missing data, not a 1000-day squeeze.
"""
import csv, datetime as dt, io

import research_store as rs

SOURCE = 'finra'
BASE = 'https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest'

# Twice-monthly settlement, published with a lag. Two cycles plus slack is the point at which a
# "latest" figure should be treated as stale rather than current.
MAX_AGE_DAYS = 45

# FINRA writes this when days-to-cover is undefined (no usable ADV).
DTC_SENTINEL = 999.99

# Plausible settlement window. Guards against the malformed rows the endpoint occasionally emits
# (one was observed with a settlement of '-24627').
EARLIEST_SETTLEMENT = dt.date(2000, 1, 1)


class ShortInterestError(Exception):
    """Short-interest data could not be retrieved or failed validation."""


def _num(x):
    if x is None or x == '':
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def _clean_dtc(v):
    """Map FINRA's 999.99 'undefined' sentinel to None."""
    if v is None:
        return None
    if abs(v - DTC_SENTINEL) < 1e-6:
        return None
    if v < 0:
        return None
    return v


def _settlement(x):
    """Return a validated ISO settlement date, or None for a malformed row."""
    s = str(x or '').strip()
    try:
        d = dt.date.fromisoformat(s[:10])
    except ValueError:
        return None
    if d < EARLIEST_SETTLEMENT or d > dt.date.today() + dt.timedelta(days=1):
        return None
    return d.isoformat()


def parse_csv(payload):
    """Parse FINRA's CSV rows into storage rows, dropping anything malformed."""
    text = payload.decode('utf-8-sig') if isinstance(payload, (bytes, bytearray)) else str(payload)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ShortInterestError('empty short-interest response')
    if 'symbolCode' not in reader.fieldnames:
        raise ShortInterestError('unexpected short-interest schema (no symbolCode column)')
    rows, dropped = [], 0
    for r in reader:
        sym = (r.get('symbolCode') or '').strip().upper()
        settle = _settlement(r.get('settlementDate'))
        if not sym or not settle:
            dropped += 1
            continue
        shares = _num(r.get('currentShortPositionQuantity'))
        prev = _num(r.get('previousShortPositionQuantity'))
        adv = _num(r.get('averageDailyVolumeQuantity'))
        dtc = _clean_dtc(_num(r.get('daysToCoverQuantity')))
        # Recompute only when the provider gave no usable figure and the inputs allow it.
        if dtc is None and shares is not None and adv and adv > 0:
            dtc = shares / adv
        rows.append({'symbol': sym, 'settlement': settle, 'shares': shares,
                     'days_to_cover': dtc, 'avg_daily_volume': adv,
                     'previous_shares': prev,
                     'revision_flag': (r.get('revisionFlag') or '').strip() or None})
    if not rows:
        raise ShortInterestError('short-interest response had no usable rows (%d malformed)' % dropped)
    rows.sort(key=lambda r: (r['settlement'], r['symbol']))
    return rows, dropped


def fetch(limit=5000):
    """Fetch one page. No narrowing parameters are sent, because the API ignores them.

    An explicit ``Accept: text/csv`` makes this endpoint return HTTP 400, so it is omitted and the
    response is parsed as CSV, which is what it actually sends.
    """
    import urllib.error
    import urllib.request
    from sources import guard
    url = '%s?limit=%d' % (BASE, int(limit))
    guard(url.startswith('https://'), 'HTTPS required')
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Market Lens Research (local research; contact: research@example.invalid)',
        'Accept-Encoding': 'identity'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            guard(r.status == 200, 'unexpected status %d' % r.status)
            body = r.read()
    except urllib.error.HTTPError as e:
        raise ShortInterestError('HTTP %d from FINRA' % e.code) from None
    except OSError as e:
        raise ShortInterestError('network error: %s' % str(e)[:120]) from None
    rows, dropped = parse_csv(body)
    return rows, body, url, dropped


def freshness(rows, today=None):
    """Assess whether the newest settlement can be presented as the current short position."""
    if not rows:
        return {'current': False, 'reason': 'no rows returned', 'newest_settlement': None,
                'age_days': None, 'max_age_days': MAX_AGE_DAYS}
    newest = max(r['settlement'] for r in rows)
    today = today or dt.date.today()
    try:
        age = (today - dt.date.fromisoformat(newest)).days
    except ValueError:
        return {'current': False, 'reason': 'unparseable settlement date', 'newest_settlement': newest,
                'age_days': None, 'max_age_days': MAX_AGE_DAYS}
    if age <= MAX_AGE_DAYS:
        return {'current': True, 'reason': None, 'newest_settlement': newest,
                'age_days': age, 'max_age_days': MAX_AGE_DAYS}
    return {'current': False, 'newest_settlement': newest, 'age_days': age,
            'max_age_days': MAX_AGE_DAYS,
            'reason': ('newest settlement is %d days old (expected within %d); the FINRA endpoint '
                       'ignores sort and filter parameters and returns its oldest page, so the '
                       'current cycle cannot be requested' % (age, MAX_AGE_DAYS))}


def collect(c, symbols, fetched_utc, limit=5000, today=None):
    """Store one page and report it with an explicit freshness verdict."""
    rows, body, url, dropped = fetch(limit=limit)
    h = rs.put_raw(c, SOURCE, url, body, fetched_utc)
    n = rs.put_short_interest(c, rows, SOURCE, fetched_utc)
    fresh = freshness(rows, today=today)
    wanted = {s.strip().upper() for s in (symbols or []) if s and s.strip()}
    present = sorted({r['symbol'] for r in rows})
    return {
        'rows': n,
        'dropped_rows': dropped,
        'raw_sha256': h,
        'oldest_settlement': min(r['settlement'] for r in rows),
        'newest_settlement': fresh['newest_settlement'],
        'age_days': fresh['age_days'],
        'current': fresh['current'],
        'staleness_reason': fresh['reason'],
        'requested_symbols': sorted(wanted),
        'requested_found': sorted(wanted & set(present)),
        'requested_missing': sorted(wanted - set(present)),
        'stored_symbols': len(present),
        'borrow_rates_available': False,
        'borrow_rates_note': ('FINRA does not publish stock-loan borrow fees; that is a commercial '
                              'dataset, so borrow cost is reported as unavailable rather than '
                              'estimated from short interest.'),
    }


def describe(report):
    if not report:
        return 'no short-interest report'
    state = 'current' if report.get('current') else 'STALE'
    parts = ['FINRA short interest (%s)' % state,
             '%d rows, settlements %s..%s' % (report.get('rows', 0),
                                             report.get('oldest_settlement'),
                                             report.get('newest_settlement'))]
    if not report.get('current') and report.get('staleness_reason'):
        parts.append(report['staleness_reason'])
    parts.append('borrow fees unavailable')
    return '; '.join(parts)
