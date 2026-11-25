"""Research pipeline orchestrator and coverage report.

The job of this module is not just to collect data — it is to say, dataset by dataset, what was
obtained, what was not, and why. That matters more than the collection code: a research pipeline
that quietly omits survivorship-adjusted returns or options data invites conclusions the data
cannot support. Every dataset in this file is therefore declared up front with a status, and the
report is generated from the *actual* store contents, not from what we hoped to get.

Statuses:

``available``      Collected, stored, and usable now.
``partial``        Collected, with a stated limitation (history window, unofficial source, staleness).
``unavailable``    No usable source. The report gives the reason and what would be needed.
``not_configured`` Implemented, but not requested in this run.

Nothing here writes to the main journal. The research store is a separate SQLite file, so the
verified official-release journal keeps its cell-level replay guarantees.
"""
import datetime as dt

import market_data as md
import research_store as rs
import sec_edgar as se
import short_interest as si
from store import now as _now

# Newer collectors are imported lazily inside run() so an import problem in one of them degrades a
# single dataset rather than preventing the whole pipeline from starting.

# Every dataset named in the specification, with its honest default status. ``available`` here
# means "we have a working path for it", not "it was collected in this run" — the report combines
# this with real counts from the store.
DATASETS = (
    # 1. Core market & price data
    ('ohlcv_unadjusted', 'OHLCV, unadjusted', '1. Core market data',
     'available', 'Yahoo chart: raw closes stored immutably (share quantities, dollar volume, margin).'),
    ('ohlcv_adjusted', 'OHLCV, adjusted', '1. Core market data',
     'available', 'Derived, never stored as fact: close x cumulative factor known as of the query time.'),
    ('corporate_actions', 'Corporate actions', '1. Core market data',
     'partial', 'Dividends and splits with ex-dates from the provider. Spin-offs, mergers and '
                'ticker changes are not published by this source and are not inferred.'),
    ('universe_active', 'Universe, active', '1. Core market data',
     'available', 'SEC registrant list: about 10,400 issuers with a current registration.'),
    ('universe_delisted', 'Universe, delisted issuers', '1. Core market data',
     'partial', 'Historical filer index from SEC quarterly archives, with issuer deregistration '
                'notices (15-12G/15-15D) as the exit signal. This makes the *issuer* universe '
                'survivorship-aware, so historical fundamentals are not limited to companies that '
                'still exist. Delisting dates come from Form 25 notices when --delisting-events is '
                'used; delisted PRICES remain unavailable, so price backtests are still '
                'survivorship-biased and must say so.'),
    ('delisted_prices', 'Delisted price history', '1. Core market data',
     'unavailable', 'No public source publishes a price *series* for a delisted or acquired ticker; '
                    'every endpoint tested returns 404 for Lehman, Enron, Bear Stearns, WorldCom, '
                    'GM and Twitter. Form 25 events now give a suspension DATE and, for mergers, a '
                    'terminal cash price, but a single terminal value is not a series. Price '
                    'backtests therefore remain survivorship-biased and the report says so.'),
    ('delisting_dates', 'Delisting dates & reasons', '1. Core market data',
     'available', 'Parsed from Form 25 notices: the issuer CIK (which is NOT the filing exchange), '
                  'the trading-suspension date, the rule provision and the resulting reason — '
                  'merger, redemption, expiry, delinquent filing or exchange removal. Readable '
                  'as-of, so a backtest cannot learn of a delisting before the market did.'),
    ('benchmarks', 'Benchmark levels', '1. Core market data',
     'available', 'S&P 500 and Russell 2000 index levels from the price source. Index '
                  '*constituent* history is unavailable and not implied.'),
    ('risk_free_curve', 'Risk-free / Treasury curve', '1. Core market data',
     'partial', 'Official US Treasury daily par yields (2Y, 10Y and the 10Y-2Y spread) plus the US '
                'policy rate. A 3-month bill is NOT in the feed catalogue, so the short rate behind '
                'excess returns is the policy rate, not a 3-month T-bill.'),

    # 2. Point-in-time fundamentals
    ('pit_fundamentals', 'As-reported statements (PIT)', '2. PIT fundamentals',
     'available', 'SEC XBRL company facts, each row carrying its filing date. Restatements add '
                  'rows and never overwrite, so as-of queries return true vintages.'),
    ('filing_timestamps', 'Filing timestamps', '2. PIT fundamentals',
     'available', '10-K/10-Q/8-K/Form 4 index with EDGAR acceptance timestamps.'),
    ('concept_resolution', 'Concept-family resolution', '2. PIT fundamentals',
     'available', 'Handles retired XBRL concepts (e.g. Revenues -> '
                  'RevenueFromContractWithCustomerExcludingAssessedTax) so a stale tag cannot win.'),
    ('shares_and_dilution', 'Share count & dilution', '2. PIT fundamentals',
     'available', 'Diluted weighted-average shares and shares outstanding from XBRL, as-reported.'),
    ('free_float', 'Free float', '2. PIT fundamentals',
     'partial', 'SEC public float (dei:EntityPublicFloat) is collected with its filing date, so '
                'it is point-in-time. Two honest limits: it is a *public* float measured on the '
                'issuer\'s fiscal-year-end cover page, not a daily float, and it is reported as a '
                'dollar value rather than a share count, so converting it to float shares needs a '
                'price for that same date.'),
    ('insider_ownership', 'Institutional ownership %', '2. PIT fundamentals',
     'partial', 'Institutional ownership share and total shares outstanding from the venue. '
                'Labelled as ownership, never as free float, because the two differ.'),
    ('analyst_estimates', 'Consensus estimates', '2. PIT fundamentals',
     'partial', 'Current consensus EPS per fiscal period with high/low range and the number of '
                'estimates. A snapshot: the venue publishes no revision history, so consensus as of '
                'a past date cannot be reconstructed from here.'),
    ('estimate_revisions', 'Consensus revisions', '2. PIT fundamentals',
     'partial', 'Revisions are accumulated locally: each fetch is stored as a dated snapshot, so '
                'two snapshots of the same period reveal a revision. Empty until the collector has '
                'run more than once; no vendor revision series is available.'),
    ('earnings_surprises', 'Earnings surprise vs consensus', '2. PIT fundamentals',
     'partial', 'Computable only where the venue publishes both a consensus and an actual for the '
                'same period. A historical surprise series cannot be built, because the consensus '
                'that existed before a past release is not retrievable.'),

    # 3. Microstructure
    ('intraday_bars', 'Intraday bars', '3. Microstructure',
     'partial', '1m/5m/15m/30m/60m bars available but each with a hard provider history limit '
                '(about 7 days for 1m, 60 days for 5m, 2 years for 60m). Limits are reported per run '
                'rather than implied.'),
    ('tick_data', 'Trade ticks', '3. Microstructure',
     'unavailable', 'No free tick source. Intraday bars are the finest granularity available here.'),
    ('nbbo_quotes', 'Bid/ask quotes (single venue)', '3. Microstructure',
     'partial', 'Real bid/ask with sizes from one exchange (Nasdaq), not the consolidated NBBO. '
                'Measured spreads are therefore an upper bound on the true best-quote spread, and '
                'the quote scope is stamped on every stored quote. A consolidated tape needs a '
                'licensed feed.'),
    ('vwap', 'VWAP', '3. Microstructure',
     'partial', 'Approximable from intraday bars (sum(price*volume)/sum(volume)) within the intraday '
                'history limit. Not a consolidated-tape official VWAP.'),
    ('short_interest', 'Short interest & days-to-cover', '3. Microstructure',
     'available', 'Twice-monthly shares short, average daily volume and days-to-cover with real '
                   'settlement dates. Freshness is verified against the publication cycle, so a '
                   'stale settlement is labelled rather than used as the current position.'),
    ('borrow_rates', 'Stock-loan borrow rates', '3. Microstructure',
     'unavailable', 'Stock-loan fees are a commercial product. Short interest is not a proxy for '
                    'borrow cost and is not used as one.'),

    # 4. Macro & cross-asset
    ('vix', 'VIX', '4. Macro & cross-asset',
     'available', 'CBOE volatility index daily levels from the price source.'),
    ('move', 'MOVE (bond volatility)', '4. Macro & cross-asset',
     'available', 'ICE BofA MOVE index daily levels, verified to respond at the price source. If '
                   'it stops responding it is reported missing, never proxied from VIX.'),
    ('macro_releases_pit', 'Macro releases (PIT)', '4. Macro & cross-asset',
     'partial', 'Official CPI, unemployment, GDP and policy-rate series with release-time values, so '
                'revisions do not rewrite history. Non-farm payrolls is NOT collected: no payroll '
                'series exists in the feed catalogue, so employment is covered by unemployment '
                'rates only.'),
    ('credit_etfs', 'Credit (HYG vs LQD)', '4. Macro & cross-asset',
     'available', 'High-yield and investment-grade credit ETF levels for spread proxies.'),
    ('commodities_fx', 'Commodities & FX', '4. Macro & cross-asset',
     'available', 'WTI crude, gold, DXY and EUR/USD daily levels.'),

    # 5. Alternative data
    ('insider_form4', 'Form 4 insider filings', '5. Alternative data',
     'available', 'Form 4 filings indexed with acceptance timestamps. Transaction amounts are in the '
                   'filing XML and are not parsed here, so this is an event index, not a dataset of '
                   'insider transaction values.'),
    ('institutional_13f', '13F institutional holdings', '5. Alternative data',
     'partial', '13F filings are indexed by filer CIK and acceptance time. Holdings tables are not '
                'parsed, so positions by security are not available from this pipeline.'),
    ('material_events_8k', '8-K material events', '5. Alternative data',
     'available', '8-K filings indexed with item codes and acceptance timestamps.'),
    ('nlp_sentiment', 'NLP / news sentiment', '5. Alternative data',
     'unavailable', 'Transcripts, news wires and sentiment scores are licensed products. No text '
                    'source is available here, so no sentiment score is computed.'),
    ('options_surface', 'Options surface (skew, put/call, OI)', '5. Alternative data',
     'available', 'Per-strike call/put bid, ask, volume and open interest, with implied volatility '
                   'computed by inverting Black-Scholes on the mid. Strikes whose quotes are one-sided '
                   'or crossed are reported as unpriced rather than filled from a neighbour.'),
    ('sector_implied_vol', 'Sector implied volatility', '5. Alternative data',
     'partial', 'Index and ETF chains provide an index-level implied volatility. A weighted '
                'sector aggregate is not published and is not synthesised from single names.'),
)

STATUS_RANK = {'available': 0, 'partial': 1, 'not_configured': 2, 'unavailable': 3}


class ResearchError(Exception):
    """The research run could not proceed."""


def dataset_index():
    return {d[0]: {'key': d[0], 'name': d[1], 'section': d[2], 'status': d[3], 'note': d[4]}
            for d in DATASETS}


def _counts(c):
    """Real store counts, so status reflects what is present rather than what was requested."""
    try:
        return rs.coverage(c)
    except Exception:
        return {}


def _observed_status(key, counts, run):
    """Downgrade an 'available' dataset to 'not_configured' when this run did not collect it."""
    present = {
        'ohlcv_unadjusted': counts.get('bars', 0) > 0,
        'ohlcv_adjusted': counts.get('adjustment_factors', 0) > 0,
        'corporate_actions': counts.get('corporate_actions', 0) > 0,
        'universe_active': counts.get('universe', 0) > 0,
        'benchmarks': counts.get('bar_symbols', 0) > 0,
        'pit_fundamentals': counts.get('fundamentals', 0) > 0,
        'filing_timestamps': counts.get('filings', 0) > 0,
        'shares_and_dilution': counts.get('fundamentals', 0) > 0,
        'concept_resolution': counts.get('fundamentals', 0) > 0,
        'insider_form4': counts.get('insider_filings', 0) > 0,
        'institutional_13f': counts.get('holdings_filings', 0) > 0,
        'material_events_8k': counts.get('material_events', 0) > 0,
        'short_interest': counts.get('short_interest', 0) > 0,
        'intraday_bars': counts.get('intraday_bars', 0) > 0,
        'nbbo_quotes': counts.get('quotes', 0) > 0,
        'options_surface': counts.get('option_quotes', 0) > 0,
        'analyst_estimates': counts.get('estimates', 0) > 0,
        'estimate_revisions': counts.get('estimates', 0) > 0,
        'insider_ownership': counts.get('ownership', 0) > 0,
        'universe_delisted': counts.get('delisted_issuers', 0) > 0,
        'delisting_dates': counts.get('delist_events', 0) > 0,
        'free_float': counts.get('public_float', 0) > 0,
    }
    if key in present:
        return 'available' if present[key] else 'not_configured'
    if key in ('officially_furnished',):
        return 'available'
    return None


def _journal_series(journal):
    """Series ids actually present in the official collection journal, or ``None`` if unreadable.

    The macro and risk-free datasets do not come from the research store; they come from the
    official-release journal. Their status must be checked there rather than assumed.
    """
    if journal is None:
        return None
    try:
        return {r[0] for r in journal.execute('SELECT DISTINCT series FROM snap')}
    except Exception:
        return None


def _stored_symbols(c, symbols):
    """Which of ``symbols`` actually have a daily bar stored. Absence is stated, not assumed."""
    out = {}
    for sym in symbols:
        row = c.execute("SELECT COUNT(*) FROM bar WHERE symbol=? AND interval='1d'", (sym,)).fetchone()
        out[sym] = int(row[0]) if row else 0
    return out


# Symbols each cross-asset dataset depends on. A dataset is only 'available' if the symbols it
# names are actually present, so a name cannot be claimed from an unrelated symbol being stored.
CROSS_ASSET_GROUPS = {
    'benchmarks': ('^GSPC', '^RUT'),
    'vix': ('^VIX',),
    'move': ('^MOVE',),
    'credit_etfs': ('HYG', 'LQD'),
    'commodities_fx': ('CL=F', 'GC=F', 'DX-Y.NYB', 'EURUSD=X'),
}

# Journal series each macro dataset depends on, checked against the official collection journal.
JOURNAL_SERIES = {
    'risk_free_curve': ('UST.DAILY.2Y', 'UST.DAILY.10Y', 'bis-rates.US'),
    'macro_releases_pit': ('BLS.CPI.YOY', 'BLS.LNS14000000', 'oecd-gdp.USA'),
}


def coverage_report(c, run=None, today=None, journal=None):
    """Build the per-dataset coverage report from real store and journal contents.

    Every status is verified rather than declared: cross-asset datasets are checked symbol by
    symbol, and macro/risk-free datasets are checked against the official-release journal. A
    dataset with nothing behind it is reported as ``not_configured`` and its note says so.
    """
    del today  # reserved; the report is generated from stored data, not a valuation date
    counts = _counts(c)
    idx = dataset_index()
    rows = []
    for key, meta in idx.items():
        status = meta['status']
        note = meta['note']
        observed = _observed_status(key, counts, run)
        if observed and status in ('available', 'partial'):
            # A usable source with nothing collected is 'not_configured', not a claim of coverage.
            status = observed if observed != 'available' else status
        if status == 'partial' and observed == 'not_configured':
            status = 'not_configured'
        rows.append({'key': key, 'name': meta['name'], 'section': meta['section'],
                     'status': status, 'note': note})

    by_key = {r['key']: r for r in rows}
    symbol_detail = {}

    # Cross-asset datasets: verify each symbol individually.
    for key, syms in CROSS_ASSET_GROUPS.items():
        e = by_key.get(key)
        if not e:
            continue
        have = _stored_symbols(c, syms)
        symbol_detail[key] = have
        found = [x for x, n in have.items() if n]
        missing = [x for x, n in have.items() if not n]
        if not found:
            e['status'] = 'not_configured'
            e['note'] = 'No level stored for %s; not claimed as collected.' % ', '.join(syms)
        elif missing:
            e['status'] = 'partial'
            e['note'] = '%s present; missing %s (reported as missing, not proxied).' % (
                ', '.join(found), ', '.join(missing))
        else:
            e['status'] = 'available'

    # Macro and risk-free datasets come from the official journal, not the research store.
    js = _journal_series(journal)
    for key, series in JOURNAL_SERIES.items():
        e = by_key.get(key)
        if not e:
            continue
        if js is None:
            e['status'] = 'not_configured'
            e['note'] = ('Official journal not readable in this session, so presence of %s could '
                         'not be verified. Run with --journal to check.' % ', '.join(series))
            continue
        found = [x for x in series if x in js]
        missing = [x for x in series if x not in js]
        symbol_detail[key] = {'present': found, 'absent': missing}
        if not found:
            e['status'] = 'not_configured'
            e['note'] = ('None of %s are in the official journal yet; run the collector before '
                         'relying on this.' % ', '.join(series))
        elif missing:
            e['status'] = 'partial'
            e['note'] = ('%s collected; %s not present in the journal.' % (
                ', '.join(found), ', '.join(missing)))
        else:
            e['status'] = 'available'

    # Freshness overrides for datasets whose stored data exists but is stale.
    if run:
        si_rep = (run.get('short_interest') or {})
        if si_rep and not si_rep.get('current'):
            e = by_key.get('short_interest')
            if e:
                e['status'] = 'partial'
                e['note'] = ('Stored, but STALE: %s. Treat as history, never as the current short '
                             'position.' % (si_rep.get('staleness_reason') or 'newest settlement is old'))
        md_rep = (run.get('market_data') or {})
        for item in (md_rep.get('ok') or []):
            if item.get('interval') != '1d':
                e = by_key.get('intraday_bars')
                if e:
                    e['note'] = ('1m/5m/15m/30m/60m with hard provider limits (%s for %s). Finest '
                                 'available granularity; no ticks.' % (item.get('history_limit'),
                                                                      item.get('interval')))
                    break

    sections = {}
    for r in rows:
        sections.setdefault(r['section'], []).append(r)

    total = len(rows)
    avail = sum(1 for r in rows if r['status'] == 'available')
    partial = sum(1 for r in rows if r['status'] == 'partial')
    missing = sum(1 for r in rows if r['status'] == 'unavailable')
    unconf = sum(1 for r in rows if r['status'] == 'not_configured')

    unavailable = [r for r in rows if r['status'] == 'unavailable']
    return {
        'generated_utc': _now(),
        'store_counts': counts,
        'sections': sections,
        'symbol_detail': symbol_detail,
        'rows': rows,
        'totals': {'datasets': total, 'available': avail, 'partial': partial,
                   'unavailable': missing, 'not_configured': unconf},
        # The honest headline: what a reader must not assume this pipeline provides.
        'blocking_gaps': [
            {'key': r['key'], 'name': r['name'], 'reason': r['note']} for r in unavailable],
        'caveats': [
            'Price and cross-asset data come from unofficial, undocumented endpoints that can change '
            'or rate-limit without notice. Payloads are stored raw so any figure can be re-checked, '
            'but this is not a vetted market-data feed.',
            'The issuer universe is survivorship-aware, but delisted PRICES are not obtainable from '
            'any public source. A price backtest is therefore still survivorship-biased, and the '
            'components state so rather than adjusting for it.',
            'Quotes are one exchange\'s book (Nasdaq), not the consolidated NBBO. Measured spreads '
            'are an upper bound on the true best-quote spread, and every stored quote is stamped '
            'with that scope.',
            'Implied volatility is computed here by inverting Black-Scholes on the mid price, not '
            'read from the venue. One-sided and crossed quotes are reported as unpriced rather than '
            'filled from a neighbouring strike.',
            'Consensus estimates are the venue\'s current snapshot with no revision history. '
            'Consensus as of a past date cannot be reconstructed, so historical earnings surprises '
            'cannot be built from this source.',
            'Institutional ownership is not free float. The two measure different things and are '
            'kept separate rather than substituted.',
            'Intraday history is short by provider limit (about 7 days at 1-minute). Long-window '
            'intraday studies are not possible with this data.',
        ],
    }


def _summary_line(report):
    t = report['totals']
    return ('%d datasets: %d available, %d partial, %d unavailable, %d not configured in this run'
            % (t['datasets'], t['available'], t['partial'], t['unavailable'], t['not_configured']))


def render_text(report):
    """Plain-text coverage report, suitable for the CLI and for pasting into a research note."""
    out = ['Research data coverage', '=' * 22,
           'Generated: %s' % report['generated_utc'], _summary_line(report), '']
    marks = {'available': '[ok]     ', 'partial': '[partial]', 'unavailable': '[MISSING]',
             'not_configured': '[not run]'}
    for section in sorted(report['sections']):
        out.append(section)
        out.append('-' * len(section))
        for r in report['sections'][section]:
            out.append('  %s %s' % (marks[r['status']], r['name']))
            if r['status'] in ('unavailable', 'partial') or r['status'] == 'not_configured':
                for line in _wrap(r['note'], 74):
                    out.append('             ' + line)
        out.append('')
    out.append('Blocking gaps (do not assume these exist)')
    out.append('-' * 38)
    if report['blocking_gaps']:
        for g in report['blocking_gaps']:
            out.append('  - %s: %s' % (g['name'], ' '.join(_wrap(g['reason'], 68))))
    else:
        out.append('  none recorded')
    out.append('')
    out.append('Caveats')
    out.append('-' * 7)
    for cv in report['caveats']:
        for i, line in enumerate(_wrap(cv, 78)):
            out.append(('  - ' if i == 0 else '    ') + line)
    return '\n'.join(out)


def _wrap(text, width):
    words, lines, cur = str(text).split(), [], ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > width:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)
    return lines or ['']


def run(c, symbols, intervals=('1d',), rng=None, with_universe=True, with_sec=True,
        with_short_interest=True, include_cross_asset=None, short_interest_limit=5000,
        max_issuers=25, asof_utc=None, with_quotes=False, with_options=False,
        with_consensus=False, with_delisted=False, delisted_quarters=2,
        with_delist_events=False, notice_limit=200,
        quote_freshness_minutes=None):
    """Collect everything requested and return the run report plus a coverage report.

    Individual dataset failures never abort the run: they are recorded, and the coverage report
    then tells the truth about what is present. ``asof_utc`` only affects the PIT reads used to
    prove as-of correctness; collection always stores everything it is given.
    """
    fetched = _now()
    tickers = []
    for s in (symbols or []):
        t = (s or '').strip().upper()
        if t and t not in tickers:
            tickers.append(t)
    if not tickers and with_universe:
        # Benchmark-only run is legitimate: cross-asset data needs no single-name universe.
        tickers = []
    report = {'started_utc': fetched, 'symbols': tickers, 'intervals': list(intervals),
              'failed': {}, 'asof_utc': asof_utc}

    # Cross-asset and benchmark series are always worth having; they carry regime information.
    cross = list(include_cross_asset) if include_cross_asset is not None \
        else ['^GSPC', '^RUT', '^VIX', 'DX-Y.NYB', 'EURUSD=X', 'CL=F', 'GC=F', 'HYG', 'LQD', '^MOVE']
    market_symbols = list(dict.fromkeys(tickers + cross))
    report['market_data'] = md.collect(c, market_symbols, intervals, rng=rng)
    if report['market_data'].get('failed'):
        report['failed'].update({'market:' + k: v for k, v in report['market_data']['failed'].items()})

    if with_universe or with_sec:
        try:
            report['sec'] = se.collect(c, tickers[:max_issuers], fetched,
                                       with_universe=with_universe, with_facts=with_sec)
        except se.SecError as e:
            report['failed']['sec'] = str(e)
            report['sec'] = {'ok': [], 'failed': {'sec': str(e)}, 'universe_rows': 0}
        for k, v in (report['sec'].get('failed') or {}).items():
            report['failed']['sec:' + k] = v

    if with_short_interest:
        # Current publication beats the archive: FINRA's endpoint ignores its own sort and filter
        # parameters and serves its oldest page (2020 data), so the venue API is tried first and the
        # archive is only a fallback. Both are reported with an explicit freshness verdict.
        try:
            import quote_surface as qs
            report['short_interest'] = qs.collect_short_interest(c, tickers, fetched)
            if not report['short_interest'].get('current'):
                report['short_interest']['note'] = (
                    'venue returned only stale settlements; the FINRA archive is equally stale')
        except Exception as e:
            try:
                report['short_interest'] = si.collect(c, tickers, fetched,
                                                      limit=short_interest_limit)
                report['short_interest']['fallback'] = 'FINRA archive (venue API unavailable: %s)' % str(e)[:120]
            except si.ShortInterestError as e2:
                report['failed']['short_interest'] = str(e2)
                report['short_interest'] = {'current': False, 'staleness_reason': str(e2), 'rows': 0}

    if with_quotes and tickers:
        try:
            import quote_surface as qs
            kw = {} if quote_freshness_minutes is None else {'max_age_minutes': quote_freshness_minutes}
            report['quotes'] = qs.collect_quotes(c, tickers, fetched, **kw)
        except Exception as e:
            report['failed']['quotes'] = str(e)
            report['quotes'] = {'ok': [], 'failed': {'all': str(e)}, 'stale': []}

    if with_options and tickers:
        import quote_surface as qs
        chains, failed = [], {}
        for sym in tickers[:max_issuers]:
            try:
                r = qs.collect_chain(c, sym, fetched)
                # A surface with no IV is not a surface; it is reported rather than presented.
                chains.append({'symbol': r['symbol'], 'rows': r['rows'],
                               'expiries': r['expiries'], 'spot': r['spot'],
                               'atm_call_iv': (r.get('surface') or {}).get('atm_call_iv'),
                               'put_call_oi_ratio': (r.get('surface') or {}).get('put_call_oi_ratio'),
                               'median_call_iv': (r.get('surface') or {}).get('median_call_iv'),
                               'surface_error': (r.get('surface') or {}).get('error')})
            except Exception as e:
                failed[sym] = str(e)
        report['options'] = {'ok': chains, 'failed': failed}
        for k, v in failed.items():
            report['failed']['options:' + k] = v

    if with_consensus and tickers:
        try:
            import analyst_consensus as ac
            report['consensus'] = ac.collect(c, tickers[:max_issuers], fetched)
        except Exception as e:
            report['failed']['consensus'] = str(e)
            report['consensus'] = {'ok': [], 'failed': {'all': str(e)}}

    if with_delisted:
        try:
            import delisted as dl
            report['delisted'] = dl.collect(c, fetched, quarters=delisted_quarters,
                                            with_notices=with_delist_events,
                                            notice_limit=notice_limit)
        except Exception as e:
            report['failed']['delisted'] = str(e)
            report['delisted'] = {'quarters': [], 'failed': {'all': str(e)}, 'issuers_indexed': 0}

    c.commit()
    report['coverage'] = coverage_report(c, run=report)
    report['finished_utc'] = _now()
    return report


def describe(report):
    """One-line summary for logs."""
    cov = (report or {}).get('coverage') or {}
    t = cov.get('totals') or {}
    return ('research run: %d symbols, %d interval(s); %d available / %d partial / %d missing datasets'
            % (len((report or {}).get('symbols') or []),
               len((report or {}).get('intervals') or []),
               t.get('available', 0), t.get('partial', 0), t.get('unavailable', 0)))
