"""Delisted-issuer history, built from SEC archives rather than from a commercial vendor.

Survivorship bias is the error that flatters every backtest: if you only test on tickers that still
exist, you have quietly excluded every bankruptcy and takeover. A commercial survivorship-free
dataset solves this properly; this module gets as close as public filings allow, and states exactly
which part of the problem is solved.

What is genuinely obtainable from EDGAR, and what each piece means:

* **A historical filer list.** ``full-index`` archives hold one index file per quarter back to 1993
  with every filing and its CIK. A CIK that filed in 2005 and never filed again, and that is absent
  from the current registrant list, is *evidence* it stopped filing. That is a real signal.
* **Deregistration dates.** Form ``15-12G`` (and ``15-15D``) is a company's notice that it is
  ending registration. Its filing date is a defensible approximation of when the issuer left the
  listed universe.
* **Filings for companies that no longer exist.** Lehman Brothers, Twitter and Activision all still
  answer on ``data.sec.gov`` by CIK, so historical facts remain retrievable after delisting.

What is **not** obtainable here, and is therefore not claimed:

* **Delisting dates.** A 15-12G says deregistration, which is not the same as the day a stock
  stopped trading. Companies delist and then deregister, sometimes months later; some deregister
  while still listed.
* **Final settlement prices.** No public feed publishes the last traded price of a bankrupt or
  acquired ticker. The price source returns 404 for delisted symbols (verified on LEHMQ, ENRNQ,
  BSC, WCOM, GMGMQ, TWTR and ATVI), so this is unavailable rather than merely hard.
* **Tickers for dead issuers.** EDGAR indexes by CIK, and a ticker is often reassigned after a
  company dies. Mapping an old CIK to the ticker it traded under is not something these files do.

So the honest summary is: this module makes the *issuer* universe survivorship-aware, which fixes
"we only looked at companies that survived" for fundamentals. It does **not** make price backtests
survivorship-free, because delisted prices are unavailable. Both halves of that sentence matter.
"""
import datetime as dt
import json
import re
import urllib.error
import urllib.request

from sources import guard
import research_store as rs

SOURCE = 'sec'
UA = 'Market Lens Research (local macro/equity research; contact: research@example.invalid)'
FULL_INDEX_ROOT = 'https://www.sec.gov/Archives/edgar/full-index/'
ROOT_INDEX = FULL_INDEX_ROOT + 'index.json'

# Forms an *issuer itself* files to end or suspend its registration. These are the right signal for
# "this company left the registered universe", because the filer is the company.
DEREGISTRATION_FORMS = ('15-12G', '15-12B', '15-15D', '15-15G')

# Form 25 is filed by the *exchange* to delist a security, so the filer CIK is the exchange (NYSE,
# Nasdaq, a fund trust) and not the company being delisted. Counting it as an issuer
# deregistration produced a candidate list made of stock exchanges, which is wrong. It is recorded
# separately and excluded from issuer attributions.
EXCHANGE_NOTICE_FORMS = ('25-NSE',)

class DelistError(Exception):
    """A delisting dataset could not be retrieved or failed validation."""


def _sec_get(url, binary=False):
    guard(url.startswith('https://'), 'HTTPS required')
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept-Encoding': 'identity', 'Accept': '*/*'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            guard(r.status == 200, 'unexpected status %d' % r.status)
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise DelistError('not found at EDGAR: %s' % url) from None
        raise DelistError('HTTP %d from EDGAR' % e.code) from None
    except OSError as e:
        raise DelistError('network error: %s' % str(e)[:120]) from None


def available_quarters():
    """List the archived quarters EDGAR exposes, newest first.

    The root index also lists non-directory entries (``xbrl.gz`` and friends), which are skipped:
    treating one as a quarter would produce a request that fails later for a confusing reason.
    """
    body = _sec_get(ROOT_INDEX)
    try:
        doc = json.loads(body)
    except (TypeError, ValueError):
        raise DelistError('full-index root is not JSON') from None
    items = ((doc or {}).get('directory') or {}).get('item') or []
    years = sorted((i.get('name') for i in items
                    if i.get('type') == 'dir' and str(i.get('name', '')).isdigit()), reverse=True)
    if not years:
        raise DelistError('full-index root listed no year directories')
    return years


def fetch_company_index(year, quarter):
    """Fetch one quarter's company index, returning ``{cik: [forms]}``.

    The file is fixed-width but the company-name column contains spaces, so columns are located by
    their header positions rather than by splitting on whitespace. That is the difference between
    reading 348,000 records correctly and mis-parsing a third of them.
    """
    url = '%s%d/QTR%d/company.idx' % (FULL_INDEX_ROOT, int(year), int(quarter))
    body = _sec_get(url).decode('latin-1', errors='replace')
    lines = body.splitlines()
    # Find the header row to learn where each column starts.
    # The description line also mentions "Company Name", so the real header is the first line that
    # carries *every* column label. Matching on one label silently selects the description.
    header_at = None
    for i, line in enumerate(lines[:20]):
        if all(lbl in line for lbl in ('Company Name', 'Form Type', 'CIK', 'Date Filed')):
            header_at = i
            break
    if header_at is None:
        raise DelistError('%d QTR%d index has no recognisable header' % (year, quarter))
    header = lines[header_at]
    cols = {}
    for name in ('Company Name', 'Form Type', 'CIK', 'Date Filed'):
        pos = header.find(name)
        if pos < 0:
            raise DelistError('%d QTR%d index is missing the %r column' % (year, quarter, name))
        cols[name] = pos
    # The company-name column is fixed-width (its width is the header position of ``Form Type``).
    # The remaining fields are taken from the *end* of the line, because the date and file name sit
    # there and the form type can contain spaces (``SC 13G/A``, ``NT 10-K``), which makes splitting
    # from the left fragile. A slice from the header's label position was tried first and failed:
    # the label starts at column 86 while its value starts at 88, so the date was truncated.
    name_width = header.find('Form Type')
    if name_width <= 0:
        raise DelistError('%d QTR%d index has an unusable column header' % (year, quarter))
    tail = re.compile(r'(\d{4}-\d{2}-\d{2})\s+(\S+)\s*$')

    out = {}
    for line in lines[header_at + 2:]:
        if not line.strip() or line.startswith('-'):
            continue
        m = tail.search(line)
        if m is None:
            continue
        filed, filename = m.group(1), m.group(2)
        head = line[:m.start()].rstrip()
        try:
            # The CIK is the last token; the form type occupies the fixed-width column between the
            # name and the CIK. Slicing the form to end-of-head included the CIK, which silently
            # produced form strings like 'D/A   1717274' and therefore zero matched deregistrations.
            cik_txt = head.rsplit(None, 1)[-1]
            cik = int(cik_txt)
            form = head[name_width:len(head) - len(cik_txt)].strip()
            name = line[:name_width].strip()
            dt.date.fromisoformat(filed)
        except (ValueError, IndexError):
            continue
        if not form or not name:
            continue
        rec = out.setdefault(cik, {'cik': cik, 'name': name, 'forms': {}, 'first_filed': filed,
                                   'last_filed': filed, 'form_files': {}})
        rec['forms'][form] = rec['forms'].get(form, 0) + 1
        # The path is needed to fetch a Form 25 that carries the delisting date. Keeping it here
        # avoids a second pass over a 300,000-line index.
        files = rec['form_files'].setdefault(form, [])
        if len(files) < 8 and filename not in files:
            files.append(filename)
        if filed < rec['first_filed']:
            rec['first_filed'] = filed
        if filed > rec['last_filed']:
            rec['last_filed'] = filed
    if not out:
        raise DelistError('%d QTR%d index yielded no records' % (year, quarter))
    return out, url


def collect_quarter(c, year, quarter, fetched_utc):
    """Index one quarter and store the issuers plus any deregistration notices."""
    makers, url = fetch_company_index(year, quarter)
    # A quarterly index is tens of megabytes. Storing every one would bloat the research database
    # for data that is re-downloadable from a stable, versioned URL, so the URL and the parsed
    # result are kept and the bulk payload is not. That is a deliberate, stated trade-off.
    stored = 0
    dereg = 0
    for cik, rec in makers.items():
        end_form = None
        for form in DEREGISTRATION_FORMS:
            if form in rec['forms']:
                end_form = form
                break
        exchange_notice = 1 if any(f in rec['forms'] for f in EXCHANGE_NOTICE_FORMS) else 0
        # Only record issuers that either deregistered this quarter or first appear now; storing
        # every filer of every quarter would just duplicate the current universe.
        c.execute(
            'INSERT OR REPLACE INTO delisted_issuer(cik,name,first_filed,last_filed,'
            'dereg_form,exchange_notice,quarter,source,fetched_utc) VALUES (?,?,?,?,?,?,?,?,?)',
            (cik, rec['name'], rec['first_filed'], rec['last_filed'], end_form, exchange_notice,
             '%dQ%d' % (year, quarter), SOURCE, rs._stamp(fetched_utc)))
        stored += 1
        if end_form:
            dereg += 1
    return {'quarter': '%dQ%d' % (year, quarter), 'issuers': stored, 'deregistrations': dereg,
            'exchange_notices': sum(1 for r in makers.values()
                                    if any(f in r['forms'] for f in EXCHANGE_NOTICE_FORMS)),
            'notice_paths': list(_notice_paths(makers)), 'url': url}


def _notice_paths(makers):
    """Filing paths of exchange notices in a parsed index, so they can be fetched for dates."""
    for rec in makers.values():
        for form, paths in (rec.get('form_files') or {}).items():
            if form in EXCHANGE_NOTICE_FORMS:
                for path in paths:
                    yield path


def collect(c, fetched_utc, quarters=4, newest=True, with_notices=True, notice_limit=None):
    """Index the most recent ``quarters`` quarters (or the oldest, for a baseline).

    A small default is deliberate: each index is tens of megabytes, and a caller asking for a
    decade of history should do so knowingly.
    """
    guard(1 <= int(quarters) <= 40, 'quarters must be in (1, 40]')
    years = available_quarters()
    got = []
    failed = {}
    # Build a descending list of (year, quarter) pairs from the newest year backwards.
    for y in years:
        for q in (4, 3, 2, 1):
            if newest:
                got.append((int(y), q))
            else:
                got.append((int(y), 5 - q))
            if len(got) >= int(quarters):
                break
        if len(got) >= int(quarters):
            break
    results = []
    by_quarter = {}
    for year, quarter in got[:int(quarters)]:
        label = '%dQ%d' % (year, quarter)
        try:
            qr = collect_quarter(c, year, quarter, fetched_utc)
            results.append(qr)
            if with_notices:
                by_quarter[label] = qr.get('notice_paths') or []
        except DelistError as e:
            failed[label] = str(e)
    events = {'events': 0, 'failed': 0, 'with_suspension_date': 0,
              'with_cash_consideration': 0, 'errors': {}}
    if with_notices:
        import delist_events as de
        for label, paths in by_quarter.items():
            if not paths:
                continue
            r = de.collect(c, fetched_utc, paths, quarter=label, limit=notice_limit)
            for k in ('events', 'failed', 'with_suspension_date', 'with_cash_consideration'):
                events[k] += r[k]
            events['errors'].update(r['errors'])
    c.commit()
    return {'source': SOURCE, 'quarters': results, 'failed': failed,
            'issuers_indexed': sum(r['issuers'] for r in results),
            'deregistrations': sum(r['deregistrations'] for r in results),
            'exchange_notices': sum(r.get('exchange_notices', 0) for r in results),
            'delist_events': events}


def delisted_candidates(c, limit=50):
    """Issuers that filed historically, deregistered, and are not current registrants.

    "Not in the current list" alone is not evidence of anything — EDGAR's registrant file only
    covers issuers with a current registration, and plenty of them are simply late filers. The
    combination of an explicit deregistration form *and* absence from the current list is what
    makes this meaningful, and both conditions are required.
    """
    sql = '''
      SELECT d.cik, d.name, d.dereg_form, d.first_filed, d.last_filed
        FROM (SELECT cik, MIN(name) AS name, MIN(dereg_form) AS dereg_form,
                     MIN(first_filed) AS first_filed, MAX(last_filed) AS last_filed
                FROM delisted_issuer WHERE dereg_form IS NOT NULL GROUP BY cik) d
       WHERE NOT EXISTS (SELECT 1 FROM universe u WHERE u.cik = d.cik)
       ORDER BY d.last_filed DESC LIMIT ?'''
    return [dict(r) for r in c.execute(sql, (int(limit),))]


def describe(report):
    if not report:
        return 'no delisting report'
    parts = ['%d issuer(s) indexed across %d quarter(s)'
             % (report.get('issuers_indexed', 0), len(report.get('quarters') or [])),
             '%d issuer deregistrations' % report.get('deregistrations', 0),
             '%d exchange notices' % report.get('exchange_notices', 0)]
    ev = report.get('delist_events') or {}
    if ev.get('events'):
        # The dates are the point: a delisting event with a suspension date is materially better
        # than an undated deregistration, so the count of dated events is reported, not the total.
        parts.append('%d delisting events (%d with a suspension date, %d with merger cash)'
                     % (ev['events'], ev.get('with_suspension_date', 0),
                        ev.get('with_cash_consideration', 0)))
    if report.get('failed'):
        parts.append('failed: ' + ', '.join(list(report['failed'])[:4]))
    # Still true, and the sentence is deliberately kept: an event record is not a price series.
    parts.append('delisted PRICE HISTORY remains unavailable; an event date and a merger cash '
                 'consideration are not a price series')
    return 'SEC full-index - ' + '; '.join(parts)
