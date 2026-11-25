"""Analyst consensus, earnings surprises, ratings and ownership share, from a public venue API.

Consensus estimates are normally an expensive licensed dataset because of how they are built: a
vendor polls brokers, timestamps each revision, and sells the result. What this module collects is
the *published snapshot* the venue displays — the current consensus EPS forecast per fiscal quarter
with the high/low range and the number of contributing estimates — plus the venue's analyst rating
summary and institutional-ownership percentage.

That is genuinely useful and it is also strictly weaker than a real estimates feed, in three ways
that must not be glossed over:

1. **It is a snapshot, not a history.** You see today's consensus, not what consensus was on the day
   before an earnings release. So a *historical* earnings surprise cannot be reconstructed from this
   source: that would require the estimate that existed at the time. Where a surprise is reported
   below, it is computed only when the venue supplies both the estimate and the actual for the same
   period, and it is labelled with the date it was computed.
2. **Revisions are not tracked.** "Estimate revisions" in the specification means the change over
   time. A single snapshot cannot show that, so no revision series is produced.
3. **Coverage is per-symbol and per-venue.** A small cap with no analyst coverage returns nothing,
   which is reported as no coverage rather than as zero estimates.

Institutional ownership is reported as a share of shares outstanding, which is **not** free float.
Float excludes insiders' and restricted holdings; the venue's "institutional ownership" is the share
held by institutions. They are different quantities and must not be substituted for one another.
"""
import json
import urllib.error
import urllib.request

from sources import guard
import research_store as rs

SOURCE = 'nasdaq'
BASE = 'https://api.nasdaq.com/api'
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0 Safari/537.36')

ASSET_CLASSES = ('stocks', 'etf')


class EstimateError(Exception):
    """A consensus or ownership dataset could not be retrieved or failed validation."""


def _get(url):
    guard(url.startswith('https://'), 'HTTPS required')
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept': 'application/json', 'Accept-Encoding': 'identity'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            guard(r.status == 200, 'unexpected status %d' % r.status)
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise EstimateError('not found: %s' % url.split('/api/')[-1]) from None
        raise EstimateError('HTTP %d from the venue' % e.code) from None
    except OSError as e:
        raise EstimateError('network error: %s' % str(e)[:120]) from None
    if not body:
        raise EstimateError('empty response from the venue')
    try:
        return json.loads(body), body
    except (TypeError, ValueError):
        raise EstimateError('response was not JSON') from None


def _num(x):
    if x is None:
        return None
    s = str(x).strip().replace('$', '').replace(',', '').replace('%', '')
    if s in ('', '--', 'N/A'):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v


def _period_key(label):
    """Turn a fiscal-period label into a sortable ``YYYY-MM`` where possible."""
    import datetime as dt
    s = str(label or '').strip()
    for fmt in ('%b %Y', '%b %d, %Y', '%B %Y', '%Y-%m-%d', '%m/%d/%Y'):
        try:
            return dt.datetime.strptime(s, fmt).strftime('%Y-%m')
        except ValueError:
            continue
    return s


def parse_forecast(payload, symbol):
    """Parse the published consensus forecast table into per-period rows."""
    data = (payload or {}).get('data')
    if not isinstance(data, dict):
        raise EstimateError('%s: no forecast data' % symbol)
    out = []
    for key in ('quarterlyForecast', 'yearlyForecast'):
        block = data.get(key)
        if not isinstance(block, dict):
            continue
        rows = block.get('rows') or []
        for r in rows:
            period = r.get('fiscalEnd') or r.get('fiscalEndDate') or ''
            eps = _num(r.get('consensusEPSForecast'))
            label = str(period).strip()
            out.append({
                'symbol': symbol,
                'kind': 'quarter' if key == 'quarterlyForecast' else 'year',
                'period': label,
                # A sortable key, because 'Dec 2027' sorts before 'Jun 2027' as a string and the
                # newest period would then be wrong.
                'period_key': _period_key(label),
                'consensus_eps': eps,
                'high_eps': _num(r.get('highEPSForecast')),
                'low_eps': _num(r.get('lowEPSForecast')),
                'num_estimates': int(_num(r.get('noOfEstimates')) or 0) or None,
                # The venue prefixes some of its own labels with an asterisk; keep its wording.
                'is_estimate': True,
            })
    if not out:
        raise EstimateError('%s: the venue published no consensus estimates' % symbol)
    return out


def parse_ratings(payload, symbol):
    """Parse the analyst rating summary: mean rating, the contributing analysts and their names."""
    data = (payload or {}).get('data')
    if not isinstance(data, dict):
        raise EstimateError('%s: no ratings data' % symbol)
    summary = data.get('ratingsSummary')
    mean = data.get('meanRatingType')
    brokers = data.get('brokerNames') or []
    if not summary and not mean and not brokers:
        raise EstimateError('%s: no analyst ratings published' % symbol)
    count = None
    if isinstance(summary, str):
        # "Based on 29 analysts offering recommendations for 'AAPL'." — the count is the number.
        digits = ''.join(ch if ch.isdigit() else ' ' for ch in summary).split()
        if digits:
            try:
                count = int(digits[0])
            except ValueError:
                count = None
    return {'symbol': symbol, 'mean_rating': mean, 'analyst_count': count,
            'summary_text': summary if isinstance(summary, str) else None,
            'brokers': brokers if isinstance(brokers, list) else [],
            'note': ('A single rating snapshot, not a revision history. The venue publishes no '
                     'timestamp for it here.')}


def parse_ownership(payload, symbol):
    """Parse institutional ownership: share of shares outstanding, plus total shares outstanding."""
    data = (payload or {}).get('data')
    if not isinstance(data, dict):
        raise EstimateError('%s: no ownership data' % symbol)
    summary = data.get('ownershipSummary')
    if not isinstance(summary, dict):
        raise EstimateError('%s: ownership payload has no summary' % symbol)
    got = {}
    for key, meta in summary.items():
        if isinstance(meta, dict):
            got[key] = meta.get('value')
    pct = _num(got.get('SharesOutstandingPCT'))
    shares_m = _num(got.get('ShareoutstandingTotal'))
    return {
        'symbol': symbol,
        'institutional_ownership_pct': pct,
        'shares_outstanding': int(shares_m * 1e6) if shares_m else None,
        # Explicit, because this is the exact substitution the specification warns about.
        'metric': 'institutional ownership as % of shares outstanding',
        'not_free_float': ('Free float excludes insider and restricted holdings; institutional '
                           'ownership does not measure float and must not be used as a proxy.'),
    }


def _try_classes(path_tpl, symbol, payload_parser):
    """Try stock then ETF paths, since the venue routes them separately."""
    last = None
    for ac in ASSET_CLASSES:
        url = path_tpl % (symbol, ac)
        try:
            doc, body = _get(url)
            parsed = payload_parser(doc, symbol)
        except EstimateError as e:
            last = e
            continue
        if isinstance(parsed, list) and not parsed:
            last = EstimateError('%s: empty result' % symbol)
            continue
        return parsed, body, url
    raise EstimateError(str(last) if last else 'no data for %s' % symbol)


FORECAST_URL = BASE + '/analyst/%s/earnings-forecast'
RATINGS_URL = BASE + '/analyst/%s/ratings'
OWNERSHIP_URL = BASE + '/company/%s/institutional-holdings?limit=5'


def fetch_forecast(symbol, asset_class=None):
    sym = (symbol or '').strip().upper()
    guard(sym and all(ch.isalnum() or ch in '.-' for ch in sym), 'invalid ticker: %r' % (symbol,))
    doc, body = _get(FORECAST_URL % sym)
    return parse_forecast(doc, sym), body, FORECAST_URL % sym


def fetch_ratings(symbol):
    sym = (symbol or '').strip().upper()
    guard(sym, 'empty ticker')
    doc, body = _get(RATINGS_URL % sym)
    return parse_ratings(doc, sym), body, RATINGS_URL % sym


def fetch_ownership(symbol):
    sym = (symbol or '').strip().upper()
    guard(sym, 'empty ticker')
    doc, body = _get(OWNERSHIP_URL % sym)
    return parse_ownership(doc, sym), body, OWNERSHIP_URL % sym


def surprise(consensus, actual, period):
    """Signed and relative surprise, only when both inputs exist for the same period.

    A consensus of exactly zero has no meaningful percentage change, so the absolute difference is
    returned and the ratio is ``None`` rather than an infinity.
    """
    if consensus is None or actual is None:
        return {'period': period, 'consensus': consensus, 'actual': actual,
                'difference': None, 'pct': None,
                'reason': 'both a consensus and an actual figure are required'}
    diff = actual - consensus
    return {'period': period, 'consensus': consensus, 'actual': actual,
            'difference': round(diff, 6),
            'pct': round(diff / abs(consensus), 6) if consensus else None,
            'reason': None if consensus else 'consensus was zero, so a percentage is undefined'}


def collect(c, symbols, fetched_utc, with_ratings=True, with_ownership=True):
    """Store consensus, ratings and ownership for each symbol, tolerating per-symbol failure."""
    out = {'source': SOURCE, 'ok': [], 'failed': {},
           'estimates_are_snapshots': True,
           'revision_history_available': False}
    for raw in symbols:
        sym = (raw or '').strip().upper()
        if not sym:
            continue
        rec = {'symbol': sym}
        try:
            rows, body, url = fetch_forecast(sym)
            rs.put_raw(c, SOURCE, url, body, fetched_utc)
            rs.put_estimate(c, rows, fetched_utc, SOURCE)
            rec['forecast_periods'] = len(rows)
        except EstimateError as e:
            out['failed'][sym] = str(e)
            continue
        if with_ratings:
            try:
                r, body, url = fetch_ratings(sym)
                rs.put_raw(c, SOURCE, url, body, fetched_utc)
                rs.put_rating(c, r, fetched_utc, SOURCE)
                rec['mean_rating'] = r.get('mean_rating')
                rec['analyst_count'] = r.get('analyst_count')
            except EstimateError as e:
                rec['ratings'] = 'unavailable: %s' % e
        if with_ownership:
            try:
                o, body, url = fetch_ownership(sym)
                rs.put_raw(c, SOURCE, url, body, fetched_utc)
                rs.put_ownership(c, o, fetched_utc, SOURCE)
                rec['institutional_ownership_pct'] = o.get('institutional_ownership_pct')
            except EstimateError as e:
                rec['ownership'] = 'unavailable: %s' % e
        out['ok'].append(rec)
    return out


def describe(report):
    if not report:
        return 'no consensus report'
    parts = ['%d symbol(s) with consensus' % len(report.get('ok') or [])]
    if report.get('failed'):
        parts.append('no coverage: ' + ', '.join(list(report['failed'])[:5]))
    parts.append('snapshot only - no revision history')
    return 'Nasdaq consensus - ' + '; '.join(parts)
