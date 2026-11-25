"""SEC EDGAR collection: point-in-time fundamentals, filings and the issuer universe.

EDGAR is the right source for this because every fact carries the date it was **filed**, not just
the period it describes. That is what makes restatements harmless here: a later 10-K adds a new row
for the same fiscal period, and an as-of query then returns exactly what was public on that day.

SEC requirements, and they are not optional:

* A descriptive ``User-Agent`` with contact details. Requests without one are refused, and a
  generic browser string is against their published policy.
* No more than ten requests per second. A conservative pace is used here.

Filings are indexed (form, filed date, acceptance timestamp, accession number) rather than parsed
into structured statements. Reconciliation between XBRL and as-presented statements is a large
project in its own right, and half-doing it would produce numbers that look authoritative and are
not.
"""
import json, re
from urllib.parse import quote

from net import fetch, FetchFailure
from sources import guard
import research_store as rs

SOURCE = 'sec'
UA = 'Market Lens Research (local macro/equity research; contact: research@example.invalid)'

TICKERS_URL = 'https://www.sec.gov/files/company_tickers.json'
FACTS_URL = 'https://data.sec.gov/api/xbrl/companyfacts/CIK%010d.json'
SUBMISSIONS_URL = 'https://data.sec.gov/submissions/CIK%010d.json'

# Forms worth indexing separately, because they answer different questions.
FORM_GROUPS = {
    '10-K': 'annual_report',
    '10-Q': 'quarterly_report',
    '8-K': 'material_event',
    '4': 'insider_transaction',
    '144': 'proposed_sale',
    '13F-HR': 'institutional_holdings',
    '13F-HR/A': 'institutional_holdings',
    'DEF 14A': 'proxy',
    'S-1': 'registration',
}

# Concepts that matter for sizing and return math. Others are stored too; these are the ones the
# coverage report checks for by name.
KEY_CONCEPTS = (
    'Revenues',
    'RevenueFromContractWithCustomerExcludingAssessedTax',
    'NetIncomeLoss',
    'EarningsPerShareDiluted',
    'Assets',
    'Liabilities',
    'StockholdersEquity',
    'NetCashProvidedByUsedInOperatingActivities',
    'WeightedAverageNumberOfDilutedSharesOutstanding',
    'CommonStockSharesOutstanding',
)


class SecError(Exception):
    """An EDGAR dataset could not be retrieved or failed validation."""


def _sec_get(url):
    """Fetch from EDGAR with the identifying User-Agent the SEC requires."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept-Encoding': 'identity',
        'Accept': 'application/json',
    })
    guard(url.startswith('https://'), 'HTTPS required')
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            guard(r.status == 200, 'unexpected status %d' % r.status)
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SecError('not found at EDGAR: %s' % url) from None
        if e.code == 403:
            raise SecError('EDGAR refused the request (403); the User-Agent must identify you') from None
        raise SecError('HTTP %d from EDGAR' % e.code) from None
    except OSError as e:
        raise SecError('network error: %s' % str(e)[:120]) from None
    if not body:
        raise SecError('empty response from EDGAR')
    return body


def parse_tickers(payload):
    """Parse company_tickers.json into universe rows."""
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise SecError('company_tickers.json is not JSON') from None
    if not isinstance(doc, dict):
        raise SecError('company_tickers.json has an unexpected shape')
    rows = []
    for item in doc.values():
        if not isinstance(item, dict):
            continue
        sym = str(item.get('ticker') or '').strip().upper()
        cik = item.get('cik_str')
        if not sym or not isinstance(cik, int):
            continue
        # EDGAR's list is issuers with current registration: absence is not proof of delisting.
        rows.append({'symbol': sym, 'cik': cik, 'title': item.get('title'), 'active': True})
    if not rows:
        raise SecError('company_tickers.json contained no usable tickers')
    return rows


def parse_companyfacts(cik, payload):
    """Parse companyfacts into PIT fundamental rows, each carrying its ``filed`` date."""
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise SecError('companyfacts is not JSON') from None
    facts = (doc or {}).get('facts')
    if not isinstance(facts, dict) or not facts:
        raise SecError('companyfacts has no facts for CIK %s' % cik)
    rows = []
    for taxonomy, concepts in facts.items():
        if not isinstance(concepts, dict):
            continue
        for concept, meta in concepts.items():
            units = (meta or {}).get('units')
            if not isinstance(units, dict):
                continue
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    end, filed = e.get('end'), e.get('filed')
                    if not end or not filed:
                        continue
                    try:
                        val = float(e.get('val'))
                    except (TypeError, ValueError):
                        val = None
                    rows.append({
                        'concept': concept, 'unit': unit, 'period_end': end, 'filed': filed,
                        'val': val, 'form': e.get('form'), 'fy': e.get('fy'), 'fp': e.get('fp'),
                        'accn': e.get('accn') or '', 'taxonomy': taxonomy,
                    })
    if not rows:
        raise SecError('companyfacts contained no dated facts for CIK %s' % cik)
    return rows


def parse_submissions(cik, payload):
    """Parse the submissions index into filing rows with EDGAR's acceptance timestamps."""
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise SecError('submissions is not JSON') from None
    recent = ((doc or {}).get('filings') or {}).get('recent')
    if not isinstance(recent, dict):
        raise SecError('submissions has no recent filings for CIK %s' % cik)
    forms = recent.get('form') or []
    rows = []
    for i, form in enumerate(forms):
        accn = (recent.get('accessionNumber') or [None] * (i + 1))[i]
        filed = (recent.get('filingDate') or [None] * (i + 1))[i]
        if not accn or not filed:
            continue
        accepted = (recent.get('acceptanceDateTime') or [None] * (i + 1))[i]
        rows.append({
            'accn': accn, 'form': form, 'filed': filed,
            'accepted_utc': accepted,
            'period': (recent.get('reportDate') or [None] * (i + 1))[i],
            'items': (recent.get('items') or [None] * (i + 1))[i],
            'primary_doc': (recent.get('primaryDocument') or [None] * (i + 1))[i],
            'is_xbrl': (recent.get('isXBRL') or [0] * (i + 1))[i],
        })
    if not rows:
        raise SecError('submissions contained no usable filings for CIK %s' % cik)
    return rows


def collect_universe(c, fetched_utc):
    body = _sec_get(TICKERS_URL)
    rows = parse_tickers(body)
    h = rs.put_raw(c, SOURCE, TICKERS_URL, body, fetched_utc)
    n = rs.put_universe(c, rows, SOURCE, fetched_utc)
    return {'universe_rows': n, 'raw_sha256': h}


def collect_issuer(c, cik, fetched_utc, include_facts=True, include_filings=True):
    """Collect one issuer's fundamentals and/or filing index."""
    out = {'cik': int(cik), 'fundamentals': 0, 'filings': 0}
    if include_facts:
        url = FACTS_URL % int(cik)
        try:
            body = _sec_get(url)
        except SecError as e:
            if 'not found' not in str(e):
                raise
            # Not a transport failure: EDGAR holds no XBRL company facts for this CIK. Funds, ETFs,
            # trusts and many foreign private issuers file but tag no us-gaap facts. Reporting that
            # plainly is correct; inventing zeros or retrying would not be.
            out['fundamentals'] = 0
            out['concepts'] = []
            out['no_xbrl_reason'] = ('EDGAR holds no XBRL company facts for CIK %d. Normal for '
                                     'funds, ETFs, trusts and many foreign issuers, which file but '
                                     'do not tag us-gaap facts.' % int(cik))
        else:
            rows = parse_companyfacts(cik, body)
            rs.put_raw(c, SOURCE, url, body, fetched_utc)
            out['fundamentals'] = rs.put_fundamentals(c, cik, rows, fetched_utc)
            out['concepts'] = sorted({r['concept'] for r in rows})
    if include_filings:
        url = SUBMISSIONS_URL % int(cik)
        try:
            body = _sec_get(url)
        except SecError as e:
            if 'not found' not in str(e):
                raise
            out['filings'] = 0
            out['no_filings_reason'] = 'EDGAR holds no submissions index for CIK %d' % int(cik)
            return out
        rows = parse_submissions(cik, body)
        rs.put_raw(c, SOURCE, url, body, fetched_utc)
        out['filings'] = rs.put_filings(c, cik, rows, fetched_utc)
        import collections
        counts = collections.Counter(r['form'] for r in rows)
        out['forms'] = dict(counts.most_common(10))
    return out


def collect(c, tickers, fetched_utc, with_universe=True, with_facts=True):
    """Collect EDGAR data for the given tickers, resolving tickers to CIKs via the universe."""
    report = {'source': SOURCE, 'ok': [], 'failed': {}, 'universe_rows': 0}
    if with_universe:
        try:
            u = collect_universe(c, fetched_utc)
            report['universe_rows'] = u['universe_rows']
        except SecError as e:
            report['failed']['universe'] = str(e)
    for t in tickers:
        sym = (t or '').strip().upper()
        row = c.execute('SELECT cik,title FROM universe WHERE symbol=?', (sym,)).fetchone()
        if row is None or row['cik'] is None:
            report['failed'][sym] = 'ticker not found in the SEC universe'
            continue
        try:
            r = collect_issuer(c, row['cik'], fetched_utc, include_facts=with_facts)
        except SecError as e:
            report['failed'][sym] = str(e)
            continue
        r['symbol'] = sym
        r['title'] = row['title']
        r['filing_lag_days'] = rs.filing_lag(c, row['cik'])
        report['ok'].append(r)
    return report


def describe(report):
    if not report:
        return 'no SEC report'
    parts = ['%d issuer(s) stored' % len(report.get('ok') or [])]
    if report.get('universe_rows'):
        parts.append('%d tickers in universe' % report['universe_rows'])
    if report.get('failed'):
        parts.append('failed: ' + ', '.join(list(report['failed'])[:6]))
    return 'SEC EDGAR - ' + '; '.join(parts)
