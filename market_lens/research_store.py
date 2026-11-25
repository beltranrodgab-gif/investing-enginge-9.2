"""Point-in-time research store, kept strictly separate from the official journal.

The one property that matters here is **point-in-time correctness**: a value is stored together
with the moment the market could have known it, and it is never overwritten. A restated figure
from a later filing therefore sits *beside* the original, not on top of it. Asking "what was
reported for fiscal 2024 as of 1 March 2025" returns only rows filed on or before that date.

This is why the research data lives in its own SQLite file rather than the collected journal. The
journal is a verified record of official economic releases (``reliability.inspect_journal``
replays every cell); mixing in a large, unofficial, continuously-revised market dataset would
destroy that guarantee.

Three tables carry the semantics:

* ``bar`` holds unadjusted OHLCV. An unadjusted close is a fact about a day and does not change.
* ``adj_factor`` holds the *cumulative* adjustment factor with the date it was known, because a
  split or dividend changes adjusted history retroactively. Adjusted close as-of =
  ``close x factor`` where the factor is the latest one known at that time.
* ``fundamental`` is keyed by ``(cik, concept, unit, end, filed, accn)``. ``filed`` is the
  dissemination date, so a query with ``asof`` reproduces what was actually visible that day.
"""
import json, os, sqlite3, tempfile, zlib
from pathlib import Path

SCHEMA_VERSION = 2
APPLICATION_ID = 0x4D4C5253  # 'MLRS'


def _stamp(x):
    """Normalise a timestamp to an ISO-8601 UTC text value.

    SQLite's built-in ``datetime`` adapters are deprecated, so every timestamp is converted to text
    before it is bound. Text also makes the ``asof_utc`` string comparisons well defined.
    """
    import datetime as _dt
    if isinstance(x, _dt.datetime):
        if x.tzinfo is None:
            raise ValueError('timestamps must be timezone-aware')
        return x.astimezone(_dt.timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    if x is None:
        raise ValueError('timestamp required')
    return str(x)


def connect(path, create=False):
    """Open the research database. ``create`` must be explicit, so a typo cannot silently start one."""
    p = Path(path)
    if not create and not p.exists():
        raise sqlite3.OperationalError('no research database at %s (run: research --init)' % p)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute('pragma journal_mode=WAL')
    c.execute('pragma synchronous=NORMAL')
    c.execute('pragma foreign_keys=ON')
    if create:
        _init(c)
    elif c.execute('pragma application_id').fetchone()[0] != APPLICATION_ID:
        c.close()
        raise sqlite3.OperationalError('%s is not a Market Lens research database' % p)
    else:
        _migrate(c)
    return c


def _migrate(c):
    """Bring an existing research database up to the current schema.

    Every table is created with ``IF NOT EXISTS``, so re-running initialisation *is* the migration:
    it adds a table a newer version introduced without touching any stored row. The version is
    recorded so a future step has somewhere to hang a real data migration.
    """
    have = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='quote'").fetchone()
    if have is None:
        _init(c)
        return
    row = c.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    if row is None or str(row[0]) != str(SCHEMA_VERSION):
        c.execute('INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)', ('schema_version', str(SCHEMA_VERSION)))
        c.commit()


def _init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT NOT NULL);

    -- Immutable daily/intraday bars. Unadjusted: a close is a fact about a day.
    CREATE TABLE IF NOT EXISTS bar(
      symbol TEXT NOT NULL, interval TEXT NOT NULL, ts INTEGER NOT NULL,
      open REAL, high REAL, low REAL, close REAL NOT NULL, volume REAL,
      source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(symbol, interval, ts)) WITHOUT ROWID;

    -- Cumulative adjustment factor, versioned by the date it became known. A split or dividend
    -- rewrites adjusted history, so the factor must be as-of queryable.
    CREATE TABLE IF NOT EXISTS adj_factor(
      symbol TEXT NOT NULL, ts INTEGER NOT NULL, factor REAL NOT NULL,
      asof_utc TEXT NOT NULL, source TEXT NOT NULL,
      PRIMARY KEY(symbol, ts, asof_utc)) WITHOUT ROWID;

    -- Corporate actions, timestamped by declaration where the provider supplies it.
    CREATE TABLE IF NOT EXISTS action(
      symbol TEXT NOT NULL, kind TEXT NOT NULL, ex_ts INTEGER NOT NULL,
      ratio REAL, amount REAL, currency TEXT, declared_utc TEXT,
      source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(symbol, kind, ex_ts)) WITHOUT ROWID;

    -- PIT fundamentals. ``filed`` is the dissemination date; ``accn`` disambiguates restatements.
    CREATE TABLE IF NOT EXISTS fundamental(
      cik INTEGER NOT NULL, concept TEXT NOT NULL, unit TEXT NOT NULL,
      period_end TEXT NOT NULL, filed TEXT NOT NULL, val REAL,
      form TEXT, fy INTEGER, fp TEXT, accn TEXT, taxonomy TEXT NOT NULL,
      fetched_utc TEXT NOT NULL,
      PRIMARY KEY(cik, taxonomy, concept, unit, period_end, filed, accn)) WITHOUT ROWID;

    -- Filings index (10-K, 10-Q, 8-K, 4, 13F ...) with the acceptance timestamp.
    CREATE TABLE IF NOT EXISTS filing(
      cik INTEGER NOT NULL, accn TEXT NOT NULL, form TEXT NOT NULL,
      filed TEXT NOT NULL, accepted_utc TEXT, period TEXT, items TEXT,
      primary_doc TEXT, is_xbrl INTEGER, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(cik, accn)) WITHOUT ROWID;

    -- Issuer universe. ``active`` is what the provider actually asserts; delisted coverage is not
    -- available from this source and is reported as such rather than implied by absence.
    CREATE TABLE IF NOT EXISTS universe(
      symbol TEXT NOT NULL, cik INTEGER, title TEXT, active INTEGER,
      source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(symbol, source)) WITHOUT ROWID;

    -- Short interest / borrow-style metrics, keyed by settlement date.
    CREATE TABLE IF NOT EXISTS short_interest(
      symbol TEXT NOT NULL, settlement TEXT NOT NULL, shares REAL, days_to_cover REAL,
      avg_daily_volume REAL, previous_shares REAL, revision_flag TEXT,
      source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(symbol, settlement)) WITHOUT ROWID;

    -- Quotes, kept as an append-only time series keyed by the venue's own timestamp. A bid/ask is
    -- a fact about an instant, so a later quote never overwrites a stored one.
    CREATE TABLE IF NOT EXISTS quote(
      symbol TEXT NOT NULL, venue_ts TEXT NOT NULL,
      bid REAL, ask REAL, bid_size INTEGER, ask_size INTEGER, last REAL, volume REAL,
      venue_realtime INTEGER, quote_scope TEXT NOT NULL,
      source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(symbol, venue_ts, source)) WITHOUT ROWID;

    -- Options chain rows, keyed by expiry and strike. Open interest is a snapshot per fetch.
    CREATE TABLE IF NOT EXISTS option_quote(
      symbol TEXT NOT NULL, expiry TEXT NOT NULL, strike REAL NOT NULL,
      call_bid REAL, call_ask REAL, call_last REAL, call_volume INTEGER, call_oi INTEGER,
      put_bid REAL, put_ask REAL, put_last REAL, put_volume INTEGER, put_oi INTEGER,
      source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(symbol, expiry, strike, source, fetched_utc)) WITHOUT ROWID;

    -- Issuers seen in archived quarterly filing indexes, with any deregistration notice. Used to
    -- make the *issuer* universe survivorship-aware. Delisting dates and final prices are not
    -- obtainable from public filings and are not claimed here.
    CREATE TABLE IF NOT EXISTS delisted_issuer(
      cik INTEGER NOT NULL, name TEXT, first_filed TEXT, last_filed TEXT,
      dereg_form TEXT, exchange_notice INTEGER NOT NULL DEFAULT 0,
      quarter TEXT NOT NULL, source TEXT NOT NULL, fetched_utc TEXT NOT NULL,
      PRIMARY KEY(cik, quarter)) WITHOUT ROWID;

    -- Consensus estimates, as published by the venue on the fetch date. A snapshot, not a
    -- revision history: the row records when it was seen so a later snapshot can be compared.
    CREATE TABLE IF NOT EXISTS estimate(
      symbol TEXT NOT NULL, kind TEXT NOT NULL, period TEXT NOT NULL, period_key TEXT,
      consensus_eps REAL, high_eps REAL, low_eps REAL, num_estimates INTEGER,
      seen_utc TEXT NOT NULL, source TEXT NOT NULL,
      PRIMARY KEY(symbol, kind, period, seen_utc)) WITHOUT ROWID;

    -- Analyst rating snapshot, with the venue's own wording preserved.
    CREATE TABLE IF NOT EXISTS rating(
      symbol TEXT NOT NULL, seen_utc TEXT NOT NULL, mean_rating TEXT, analyst_count INTEGER,
      summary_text TEXT, source TEXT NOT NULL,
      PRIMARY KEY(symbol, seen_utc)) WITHOUT ROWID;

    -- Ownership. ``metric`` states which quantity the percentage measures, because institutional
    -- ownership is not free float.
    CREATE TABLE IF NOT EXISTS ownership(
      symbol TEXT NOT NULL, seen_utc TEXT NOT NULL, institutional_pct REAL,
      shares_outstanding INTEGER, metric TEXT NOT NULL, source TEXT NOT NULL,
      PRIMARY KEY(symbol, seen_utc)) WITHOUT ROWID;

    -- Delisting events parsed from Form 25 (notificationOfRemoval). ``issuer_cik`` is the company
    -- the exchange delisted, which is NOT the filer: the form is filed by the exchange. This is a
    -- genuine event record with a suspension date, not a price history.
    CREATE TABLE IF NOT EXISTS delist_event(
      issuer_cik INTEGER NOT NULL, security_class TEXT, exchange_name TEXT,
      rule TEXT, effective_date TEXT, suspension_date TEXT, cash_per_share REAL, reason TEXT,
      filing_url TEXT NOT NULL, quarter TEXT NOT NULL, source TEXT NOT NULL,
      fetched_utc TEXT NOT NULL,
      PRIMARY KEY(issuer_cik, security_class, filing_url)) WITHOUT ROWID;

    -- Raw payload provenance, compressed, so a parse can always be re-checked.
    CREATE TABLE IF NOT EXISTS raw(
      h TEXT PRIMARY KEY, source TEXT NOT NULL, url TEXT NOT NULL,
      bytes INTEGER NOT NULL, zipped BLOB NOT NULL, fetched_utc TEXT NOT NULL);

    CREATE INDEX IF NOT EXISTS ix_bar_sym ON bar(symbol, interval, ts);
    CREATE INDEX IF NOT EXISTS ix_fund_cik ON fundamental(cik, concept, filed);
    CREATE INDEX IF NOT EXISTS ix_filing_cik ON filing(cik, filed);
    CREATE INDEX IF NOT EXISTS ix_adj_sym ON adj_factor(symbol, ts);
    CREATE INDEX IF NOT EXISTS ix_quote_sym ON quote(symbol, venue_ts);
    CREATE INDEX IF NOT EXISTS ix_opt_sym ON option_quote(symbol, expiry, strike);
    CREATE INDEX IF NOT EXISTS ix_delist_cik ON delisted_issuer(cik, last_filed);
    CREATE INDEX IF NOT EXISTS ix_devent_cik ON delist_event(issuer_cik, suspension_date);
    """)
    c.execute('pragma application_id=%d' % APPLICATION_ID)
    c.execute('INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)', ('schema_version', str(SCHEMA_VERSION)))
    c.commit()


def put_raw(c, source, url, body, fetched_utc):
    """Store a payload once, keyed by content hash. Returns the hash."""
    import hashlib
    h = hashlib.sha256(body).hexdigest()
    c.execute('INSERT OR IGNORE INTO raw(h,source,url,bytes,zipped,fetched_utc) VALUES (?,?,?,?,?,?)',
              (h, source, url, len(body), zlib.compress(body, 6), fetched_utc))
    return h


def put_bars(c, symbol, interval, bars, source, fetched_utc):
    """Insert bars without ever overwriting an existing unadjusted close.

    A bar that is already stored is left alone. Prices can be corrected by a provider, and when
    they are, the honest handling is to keep the original and record the new one separately
    rather than to rewrite history silently. Corrections are surfaced by ``bar_conflicts``.
    """
    rows = 0
    for b in bars:
        cur = c.execute(
            'INSERT OR IGNORE INTO bar(symbol,interval,ts,open,high,low,close,volume,source,fetched_utc)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (symbol, interval, int(b['ts']), b.get('open'), b.get('high'), b.get('low'),
             b['close'], b.get('volume'), source, fetched_utc))
        rows += cur.rowcount
    return rows


def bar_conflicts(c, symbol, interval, bars, tol=1e-6):
    """Bars that already exist with a *different* close. These are reported, not applied."""
    out = []
    for b in bars:
        r = c.execute('SELECT close FROM bar WHERE symbol=? AND interval=? AND ts=?',
                      (symbol, interval, int(b['ts']))).fetchone()
        if r is not None and abs(float(r['close']) - float(b['close'])) > tol:
            out.append({'ts': int(b['ts']), 'stored': float(r['close']), 'incoming': float(b['close'])})
    return out


def put_adj_factors(c, symbol, factors, asof_utc, source):
    """Store cumulative adjustment factors versioned by when they became known."""
    n = 0
    for ts, factor in factors:
        cur = c.execute(
            'INSERT OR REPLACE INTO adj_factor(symbol,ts,factor,asof_utc,source) VALUES (?,?,?,?,?)',
            (symbol, int(ts), float(factor), asof_utc, source))
        n += cur.rowcount
    return n


def put_actions(c, symbol, actions, source, fetched_utc):
    n = 0
    for a in actions:
        cur = c.execute(
            'INSERT OR REPLACE INTO action(symbol,kind,ex_ts,ratio,amount,currency,declared_utc,source,fetched_utc)'
            ' VALUES (?,?,?,?,?,?,?,?,?)',
            (symbol, a['kind'], int(a['ex_ts']), a.get('ratio'), a.get('amount'),
             a.get('currency'), a.get('declared_utc'), source, fetched_utc))
        n += cur.rowcount
    return n


def put_fundamentals(c, cik, rows, fetched_utc):
    """Insert PIT fundamentals. A restatement adds a row; it never replaces the original."""
    n = 0
    for r in rows:
        cur = c.execute(
            'INSERT OR REPLACE INTO fundamental(cik,concept,unit,period_end,filed,val,form,fy,fp,accn,taxonomy,fetched_utc)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (int(cik), r['concept'], r['unit'], r['period_end'], r['filed'], r.get('val'),
             r.get('form'), r.get('fy'), r.get('fp'), r.get('accn'), r['taxonomy'], fetched_utc))
        n += cur.rowcount
    return n


def put_filings(c, cik, rows, fetched_utc):
    n = 0
    for r in rows:
        cur = c.execute(
            'INSERT OR REPLACE INTO filing(cik,accn,form,filed,accepted_utc,period,items,primary_doc,is_xbrl,fetched_utc)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?)',
            (int(cik), r['accn'], r['form'], r['filed'], r.get('accepted_utc'), r.get('period'),
             r.get('items'), r.get('primary_doc'), 1 if r.get('is_xbrl') else 0, _stamp(fetched_utc)))
        n += cur.rowcount
    return n


def put_universe(c, rows, source, fetched_utc):
    n = 0
    for r in rows:
        cur = c.execute(
            'INSERT OR REPLACE INTO universe(symbol,cik,title,active,source,fetched_utc) VALUES (?,?,?,?,?,?)',
            (r['symbol'], r.get('cik'), r.get('title'), 1 if r.get('active', True) else 0,
             source, _stamp(fetched_utc)))
        n += cur.rowcount
    return n


def put_short_interest(c, rows, source, fetched_utc):
    n = 0
    for r in rows:
        cur = c.execute(
            'INSERT OR REPLACE INTO short_interest(symbol,settlement,shares,days_to_cover,avg_daily_volume,previous_shares,revision_flag,source,fetched_utc)'
            ' VALUES (?,?,?,?,?,?,?,?,?)',
            (r['symbol'], r['settlement'], r.get('shares'), r.get('days_to_cover'),
             r.get('avg_daily_volume'), r.get('previous_shares'), r.get('revision_flag'),
             source, _stamp(fetched_utc)))
        n += cur.rowcount
    return n


def put_quote_asof(c, rec, fetched_utc):
    """Store one quote. Returns conflicts (a *different* price for the same venue timestamp).

    Two quotes for the same venue timestamp with different prices mean the venue revised a tick.
    The original is kept and the difference is reported, matching how bars are handled.
    """
    venue_ts = rec.get('venue_timestamp')
    if not venue_ts:
        raise ValueError('a quote needs the venue timestamp to be storable')
    existing = c.execute(
        'SELECT bid, ask, last FROM quote WHERE symbol=? AND venue_ts=? AND source=?',
        (rec['symbol'], str(venue_ts), SOURCE_OF(rec))).fetchone()
    conflicts = []
    if existing is not None:
        for field in ('bid', 'ask', 'last'):
            old, new = existing[field], rec.get(field)
            if old is not None and new is not None and abs(float(old) - float(new)) > 1e-9:
                conflicts.append({'field': field, 'stored': float(old), 'incoming': float(new),
                                  'venue_ts': str(venue_ts)})
        if conflicts:
            return conflicts
    c.execute(
        'INSERT OR IGNORE INTO quote(symbol,venue_ts,bid,ask,bid_size,ask_size,last,volume,'
        'venue_realtime,quote_scope,source,fetched_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (rec['symbol'], str(venue_ts), rec.get('bid'), rec.get('ask'), rec.get('bid_size'),
         rec.get('ask_size'), rec.get('last'), rec.get('volume'),
         1 if rec.get('venue_realtime') else 0,
         rec.get('quote_scope') or 'unspecified',
         SOURCE_OF(rec), _stamp(fetched_utc)))
    return conflicts


def SOURCE_OF(rec):
    """The reporting source for a record. Present on every quote record we build."""
    return rec.get('source') or 'nasdaq'


def put_option_chain(c, symbol, rows, fetched_utc, source='nasdaq'):
    n = 0
    for r in rows:
        cur = c.execute(
            'INSERT OR REPLACE INTO option_quote(symbol,expiry,strike,call_bid,call_ask,call_last,'
            'call_volume,call_oi,put_bid,put_ask,put_last,put_volume,put_oi,source,fetched_utc)'
            ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (symbol, str(r.get('expiry') or ''), float(r['strike']),
             r.get('call_bid'), r.get('call_ask'), r.get('call_last'), r.get('call_volume'),
             r.get('call_oi'), r.get('put_bid'), r.get('put_ask'), r.get('put_last'),
             r.get('put_volume'), r.get('put_oi'), source, _stamp(fetched_utc)))
        n += cur.rowcount
    return n


def latest_quotes(c, symbols=None):
    """Newest stored quote per symbol, for reporting rather than trading."""
    out = {}
    sql = ('SELECT symbol, venue_ts, bid, ask, bid_size, ask_size, last, volume, quote_scope '
           'FROM quote')
    args = []
    if symbols:
        sql += ' WHERE symbol IN (%s)' % ','.join('?' * len(symbols))
        args = list(symbols)
    for r in c.execute(sql + ' ORDER BY symbol, venue_ts', args):
        out[r['symbol']] = dict(r)  # later rows win, so the newest timestamp is kept
    return out


def put_estimate(c, rows, fetched_utc, source):
    n = 0
    for r in rows:
        cur = c.execute(
            'INSERT OR REPLACE INTO estimate(symbol,kind,period,period_key,consensus_eps,high_eps,low_eps,'
            'num_estimates,seen_utc,source) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (r['symbol'], r['kind'], r['period'], r.get('period_key'), r.get('consensus_eps'),
             r.get('high_eps'), r.get('low_eps'), r.get('num_estimates'), _stamp(fetched_utc), source))
        n += cur.rowcount
    return n


def put_rating(c, r, fetched_utc, source):
    c.execute(
        'INSERT OR REPLACE INTO rating(symbol,seen_utc,mean_rating,analyst_count,summary_text,source)'
        ' VALUES (?,?,?,?,?,?)',
        (r['symbol'], _stamp(fetched_utc), r.get('mean_rating'), r.get('analyst_count'),
         r.get('summary_text'), source))
    return 1


def put_ownership(c, o, fetched_utc, source):
    c.execute(
        'INSERT OR REPLACE INTO ownership(symbol,seen_utc,institutional_pct,shares_outstanding,'
        'metric,source) VALUES (?,?,?,?,?,?)',
        (o['symbol'], _stamp(fetched_utc), o.get('institutional_ownership_pct'),
         o.get('shares_outstanding'), o.get('metric') or 'unspecified', source))
    return 1


def estimate_history(c, symbol, kind='quarter'):
    """Every stored snapshot for a symbol, oldest first, so successive consensus can be compared.

    This is how revision *observations* are made possible: each fetch is a snapshot, and two
    snapshots of the same period taken on different days show a revision. The venue itself does not
    supply that history; it is accumulated here and is empty until the collector runs more than once.
    """
    return [dict(r) for r in c.execute(
        'SELECT period, period_key, consensus_eps, num_estimates, seen_utc FROM estimate '
        'WHERE symbol=? AND kind=? ORDER BY period_key, seen_utc', (symbol, kind))]


def put_delist_event(c, rec, quarter, fetched_utc, source='sec'):
    """Store one Form 25 event. Idempotent: the same notice re-fetched does not duplicate."""
    c.execute(
        'INSERT OR REPLACE INTO delist_event(issuer_cik,security_class,exchange_name,rule,'
        'effective_date,suspension_date,cash_per_share,reason,filing_url,quarter,source,'
        'fetched_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (int(rec['issuer_cik']), rec.get('security_class'), rec.get('exchange_name'),
         rec.get('rule'), rec.get('effective_date'), rec.get('suspension_date'),
         rec.get('cash_per_share'), rec.get('reason'), rec.get('filing_url') or '',
         quarter, source, _stamp(fetched_utc)))
    return 1


def delist_events_asof(c, asof, limit=200):
    """Delisting events known on or before ``asof``, newest first.

    Point-in-time here means *known*: a notice fetched today is not used for a date before it was
    filed, so a backtest cannot learn about a delisting before the market did.
    """
    return [dict(r) for r in c.execute(
        'SELECT issuer_cik, security_class, exchange_name, rule, effective_date, suspension_date,'
        ' cash_per_share, reason, filing_url FROM delist_event'
        ' WHERE COALESCE(suspension_date, effective_date, quarter) <= ?'
        ' ORDER BY COALESCE(suspension_date, effective_date) DESC LIMIT ?',
        (str(asof)[:10], int(limit)))]


# --- point-in-time reads ---------------------------------------------------------------------

def bars_asof(c, symbol, interval='1d', asof_utc=None, adjusted=True):
    """Bars as they would have been seen at ``asof_utc``, adjusted with the factors known then."""
    rows = list(c.execute(
        'SELECT * FROM bar WHERE symbol=? AND interval=? ORDER BY ts', (symbol, interval)))
    if not rows:
        return []
    if not adjusted:
        return [dict(r) for r in rows]
    # The factor known at ``asof`` is the one with the latest asof_utc not after it.
    best = {}
    if asof_utc:
        factors = c.execute('SELECT ts, factor FROM adj_factor WHERE symbol=? AND asof_utc<=? '
                            'ORDER BY asof_utc', (symbol, asof_utc))
    else:
        # No as-of time: use everything known now, latest version per bar winning.
        factors = c.execute('SELECT ts, factor FROM adj_factor WHERE symbol=? ORDER BY asof_utc',
                            (symbol,))
    for r in factors:
        best[int(r['ts'])] = float(r['factor'])
    out = []
    for r in rows:
        d = dict(r)
        f = best.get(int(d['ts']), 1.0)
        d['adj_factor'] = f
        d['adj_close'] = round(float(d['close']) * f, 6)
        out.append(d)
    return out


def fundamentals_asof(c, cik, concept=None, asof_utc=None):
    """Latest *as-reported* value per fiscal period, using only filings known by ``asof_utc``."""
    sql = 'SELECT * FROM fundamental WHERE cik=?'
    args = [int(cik)]
    if concept:
        sql += ' AND concept=?'
        args.append(concept)
    if asof_utc:
        # ``filed`` is a date string (YYYY-MM-DD); compare on the date part only.
        sql += ' AND substr(filed,1,10)<=?'
        args.append(str(asof_utc)[:10])
    sql += ' ORDER BY period_end, filed'
    latest = {}
    for r in c.execute(sql, args):
        d = dict(r)
        key = (d['concept'], d['unit'], d['period_end'])
        prev = latest.get(key)
        # Later ``filed`` wins: that is the value the market had at ``asof``.
        if prev is None or d['filed'] >= prev['filed']:
            latest[key] = d
    return list(latest.values())


# XBRL concepts are not stable across years: a company can retire one and adopt another for the
# same economic quantity (Apple moved from ``Revenues`` to
# ``RevenueFromContractWithCustomerExcludingAssessedTax``). Looking up a single concept name
# therefore returns a stale figure that still looks authoritative. These groups list the accepted
# alternatives in priority order; a resolver walks them and reports which one was used.
CONCEPT_FAMILIES = {
    'revenue': (
        'RevenueFromContractWithCustomerExcludingAssessedTax',
        'Revenues',
        'RevenueFromContractWithCustomerIncludingAssessedTax',
        'SalesRevenueNet',
    ),
    'net_income': ('NetIncomeLoss', 'ProfitLoss'),
    'eps_diluted': ('EarningsPerShareDiluted', 'EarningsPerShareBasicAndDiluted'),
    'assets': ('Assets',),
    'liabilities': ('Liabilities',),
    'equity': ('StockholdersEquity', 'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest'),
    'operating_cash_flow': ('NetCashProvidedByUsedInOperatingActivities',),
    'diluted_shares': ('WeightedAverageNumberOfDilutedSharesOutstanding',
                       'WeightedAverageNumberOfDilutedSharesOutstandingBasicAndDiluted'),
    'shares_outstanding': ('CommonStockSharesOutstanding', 'EntityCommonStockSharesOutstanding'),
}


def resolve_concept_asof(c, cik, family, asof_utc=None):
    """Return the freshest as-reported series for a concept family, and which concept supplied it.

    Picks the alternative whose newest period is most recent, not simply the first listed, so a
    retired concept cannot keep winning. Returns ``{'concept', 'rows', 'reason'}``; with no usable
    data ``concept`` is ``None`` and ``reason`` says why.
    """
    candidates = CONCEPT_FAMILIES.get(family)
    if not candidates:
        return {'family': family, 'concept': None, 'rows': [],
                'reason': 'unknown concept family %r' % (family,)}
    best = None
    tried = {}
    for concept in candidates:
        rows = fundamentals_asof(c, cik, concept, asof_utc=asof_utc)
        if not rows:
            tried[concept] = 'no rows as of that date'
            continue
        newest = max(r['period_end'] for r in rows)
        tried[concept] = '%d rows, latest %s' % (len(rows), newest)
        if best is None or newest > best[1]:
            best = (concept, newest, rows)
    if best is None:
        return {'family': family, 'concept': None, 'rows': [], 'tried': tried,
                'reason': 'no alternative concept has data as of that date'}
    concept, newest, rows = best
    return {'family': family, 'concept': concept, 'rows': sorted(rows, key=lambda r: r['period_end']),
            'latest_period': newest, 'tried': tried, 'reason': None}


def filing_lag(c, cik):
    """Median days between period end and filing date, i.e. how stale "latest" fundamentals are."""
    rows = [r for r in c.execute(
        "SELECT period, filed FROM filing WHERE cik=? AND form IN ('10-K','10-Q') "
        "AND period IS NOT NULL AND period<>'' AND filed IS NOT NULL", (int(cik),))]
    import datetime as dt
    lags = []
    for r in rows:
        try:
            a = dt.date.fromisoformat(str(r['period'])[:10])
            b = dt.date.fromisoformat(str(r['filed'])[:10])
        except ValueError:
            continue
        lag = (b - a).days
        if 0 <= lag <= 400:
            lags.append(lag)
    if not lags:
        return None
    lags.sort()
    return lags[len(lags) // 2]


def coverage(c):
    """Counts per dataset, so a report can state what is actually present."""
    q = lambda s: c.execute(s).fetchone()[0]
    return {
        'bars': q('SELECT COUNT(*) FROM bar'),
        'bar_symbols': q('SELECT COUNT(DISTINCT symbol) FROM bar'),
        'intraday_bars': q("SELECT COUNT(*) FROM bar WHERE interval<>'1d'"),
        'adjustment_factors': q('SELECT COUNT(*) FROM adj_factor'),
        'corporate_actions': q('SELECT COUNT(*) FROM action'),
        'fundamentals': q('SELECT COUNT(*) FROM fundamental'),
        'fundamental_ciks': q('SELECT COUNT(DISTINCT cik) FROM fundamental'),
        'filings': q('SELECT COUNT(*) FROM filing'),
        'insider_filings': q("SELECT COUNT(*) FROM filing WHERE form='4'"),
        'holdings_filings': q("SELECT COUNT(*) FROM filing WHERE form LIKE '13F%'"),
        'material_events': q("SELECT COUNT(*) FROM filing WHERE form='8-K'"),
        'universe': q('SELECT COUNT(*) FROM universe'),
        'short_interest': q('SELECT COUNT(*) FROM short_interest'),
        'quotes': q('SELECT COUNT(*) FROM quote'),
        'quote_symbols': q('SELECT COUNT(DISTINCT symbol) FROM quote'),
        'option_quotes': q('SELECT COUNT(*) FROM option_quote'),
        'option_symbols': q('SELECT COUNT(DISTINCT symbol) FROM option_quote'),
        'delisted_issuers': q('SELECT COUNT(DISTINCT cik) FROM delisted_issuer'),
        'deregistration_notices': q('SELECT COUNT(*) FROM delisted_issuer WHERE dereg_form IS NOT NULL'),
        'estimates': q('SELECT COUNT(*) FROM estimate'),
        'estimate_symbols': q('SELECT COUNT(DISTINCT symbol) FROM estimate'),
        'ratings': q('SELECT COUNT(*) FROM rating'),
        'ownership': q('SELECT COUNT(*) FROM ownership'),
        'delist_events': q('SELECT COUNT(*) FROM delist_event'),
        'delist_events_dated': q("SELECT COUNT(*) FROM delist_event WHERE suspension_date IS NOT NULL"),
        'public_float': q("SELECT COUNT(DISTINCT cik) FROM fundamental "
                          "WHERE concept='EntityPublicFloat' AND val IS NOT NULL"),
        'raw_payloads': q('SELECT COUNT(*) FROM raw'),
    }
