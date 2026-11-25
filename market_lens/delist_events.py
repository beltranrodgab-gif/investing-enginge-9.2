"""Form 25 delisting events: the issuer, the date and the reason, from the notice itself.

This module exists because of a nuance that is easy to get wrong and costly to get wrong. Form
``25-NSE`` (more precisely, a ``notificationOfRemoval``) is **filed by the exchange**, so the CIK in
the filing index belongs to the NYSE or Nasdaq, not to the company being delisted. Treating the
filer as the subject produced a "delisted issuers" list made of stock exchanges and index funds —
a wrong answer that looked entirely plausible.

The filing itself separates the two explicitly::

    <exchange><cik>0000876661</cik><entityName>NEW YORK STOCK EXCHANGE LLC</entityName></exchange>
    <issuer><cik>0001637873</cik><entityName>ACV Auctions Inc.</entityName></issuer>

and the exhibit states what happened. So the notice is a genuine, dated delisting record, which is
better than anything derived from an absence of filings. What it provides:

* **The issuer**, by CIK, correctly identified.
* **A trading suspension date**, usually stated verbatim ("suspended from trading on October 16,
  2026").
* **The reason**, from the rule provision — a merger, a redemption, an expiry, delinquent filing, or
  removal under exchange rules.
* **The terminal cash consideration**, but *only* for a merger, and only because that provision
  necessarily produces a cash price per share.

What it **does not** provide, and what this module therefore does not claim:

* **A price history.** A merger's $10.50 per share is a single terminal value, not a series. For a
  delinquent delisting there is no price at all. Survivorship-free *backtesting* still needs a
  commercial dataset; this gives delisting *dates and events*, which is a different and smaller
  thing, and the output says which.
* **Coverage beyond what was filed.** A delisting with no Form 25 on EDGAR is invisible here.

Nothing is inferred. A missing suspension date is stored as ``None`` rather than being backfilled
from the filing date, because the two differ by days to weeks and a backtest would treat the
substitute as fact.
"""
import datetime as dt
import re
import urllib.error

from sources import guard
import research_store as rs

SOURCE = 'sec'
# The index lists paths as ``edgar/data/...``; the archive lives under /Archives/.
ARCHIVE = 'https://www.sec.gov/Archives/'
PLACEHOLDER = None


class NoticeError(Exception):
    """A Form 25 could not be fetched or did not contain an identifiable issuer."""


def harvest_paths(index):
    """Collect the filing paths of exchange notices from a parsed quarterly index.

    The index parser keeps up to eight paths per form per filer, which is plenty: a single exchange
    cannot realistically delist more than a handful of securities for one issuer in one quarter,
    and the cap stops a malformed file from ballooning memory.
    """
    out = []
    for cik, rec in (index or {}).items():
        for form, paths in (rec.get('form_files') or {}).items():
            if form in ('25-NSE', '25'):
                out.extend(paths)
    return out


def _iso_from_long(text):
    """Turn 'October 16, 2026' into an ISO date, or None. No guessing on a different format."""
    for fmt in ('%B %d, %Y', '%b %d, %Y'):
        try:
            return dt.datetime.strptime(text.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


RULE_REASONS = (
    ('(a)(1)', 'called for redemption'),
    ('(a)(2)', 'expired or retired'),
    ('(a)(3)', 'replaced by other securities (merger or acquisition)'),
    ('(a)(4)', 'issuer failed to file required reports'),
    ('(b)', 'removed under the exchange\'s own listing rules'),
)


def parse_notice(text):
    """Parse a Form 25 into a delisting event.

    Raises ``NoticeError`` when the issuer cannot be identified, because an event attributed to the
    wrong company is worse than no event at all.
    """
    guard(isinstance(text, str) and text.strip(), 'notice body is empty')
    out = {'issuer_cik': None, 'issuer_name': None, 'exchange_name': None,
           'security_class': None, 'rule': None, 'effective_date': None,
           'suspension_date': None, 'cash_per_share': None, 'reason': None}

    m = re.search(r'<issuer>\s*<cik>\s*0*(\d+)\s*</cik>\s*<entityName>([^<]+)</entityName>',
                  text, re.S)
    if m:
        out['issuer_cik'] = int(m.group(1))
        out['issuer_name'] = m.group(2).strip()
    else:
        # Older filings are plain text rather than XML. The subject company is still named, so
        # fall back to the SGML header rather than discarding a usable notice.
        m = re.search(r'SUBJECT COMPANY:.*?CENTRAL INDEX KEY:\s*0*(\d+)', text, re.S)
        if m:
            out['issuer_cik'] = int(m.group(1))
        m = re.search(r'COMPANY CONFORMED NAME:\s*([^\n\r]+)', text)
        if m and out['issuer_cik']:
            out['issuer_name'] = m.group(1).strip()

    if out['issuer_cik'] is None:
        raise NoticeError('no issuer CIK in the notice, so the event cannot be attributed')

    m = re.search(r'<exchange>\s*<cik>\s*0*(\d+)\s*</cik>\s*<entityName>([^<]+)</entityName>',
                  text, re.S)
    if m:
        out['exchange_name'] = m.group(2).strip()

    m = re.search(r'<descriptionClassSecurity>([^<]*)</descriptionClassSecurity>', text)
    if m:
        out['security_class'] = m.group(1).strip()

    m = re.search(r'<ruleProvision>([^<]+)</ruleProvision>', text)
    if m:
        out['rule'] = m.group(1).strip()

    m = re.search(r'<signatureDate>(\d{4}-\d{2}-\d{2})</signatureDate>', text)
    if m:
        out['effective_date'] = m.group(1)

    m = re.search(r'suspended from trading on ([A-Z][a-z]+ \d{1,2}, \d{4})', text)
    if m:
        out['suspension_date'] = _iso_from_long(m.group(1))

    if out['rule']:
        for suffix, reason in RULE_REASONS:
            if out['rule'].endswith(suffix):
                out['reason'] = reason
                break
        # A cash price is only meaningful for the replacement provision. Scanning for any dollar
        # amount would pick up a redemption or liquidation figure and present it as a terminal
        # share price, which it is not.
        if out['rule'].endswith('(a)(3)'):
            m = re.search(r'\$([\d,]+\.\d{2})\s*per share', text)
            if m:
                try:
                    out['cash_per_share'] = float(m.group(1).replace(',', ''))
                except ValueError:
                    out['cash_per_share'] = None
    return out


def collect(c, fetched_utc, paths, quarter=None, limit=None, fetcher=None):
    """Fetch notices and store one event each. Per-filing failure never aborts the batch."""
    fetch = fetcher
    if fetch is None:
        from delisted import _sec_get as fetch  # local import avoids a circular import at load
    events, errors = [], {}
    used = paths[:int(limit)] if limit else list(paths)
    for path in used:
        url = path if path.startswith('http') else ARCHIVE + str(path).lstrip('/')
        try:
            rec = parse_notice(fetch(url).decode('utf-8', errors='replace'))
        except Exception as e:  # noqa: BLE001 - one bad filing must not abort the batch
            # Deliberately broad: these are third-party documents, and a parse or transport error in
            # one notice must not stop the other 300 from being read. The error is kept per filing.
            errors[path] = str(e)[:160]
            continue
        rec['filing_url'] = url
        rs.put_delist_event(c, rec, quarter, fetched_utc, SOURCE)
        events.append(rec)
    return {'quarter': quarter, 'events': len(events), 'failed': len(errors), 'errors': errors,
            'with_suspension_date': sum(1 for r in events if r['suspension_date']),
            'with_cash_consideration': sum(1 for r in events if r['cash_per_share'])}


def asof(c, date):
    """Delisting events known on or before ``date``, newest first."""
    return rs.delist_events_asof(c, date)


def describe(report):
    """One-line honesty summary. The last clause matters as much as the counts."""
    if not report:
        return 'no delisting events'
    parts = ['%d event(s)' % report.get('events', 0),
             '%d with a trading-suspension date' % report.get('with_suspension_date', 0),
             '%d with a merger cash price' % report.get('with_cash_consideration', 0)]
    if report.get('failed'):
        parts.append('%d unreadable' % report['failed'])
    return ('SEC Form 25 - ' + '; '.join(parts)
            + '; a terminal price is not a price history, so price backtests stay survivorship-biased')
