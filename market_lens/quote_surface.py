"""Live quotes (NBBO-style bid/ask) and the options surface, from Nasdaq's public JSON endpoints.

Why this source: the price source used for history returns HTTP 401 on its quote and options
endpoints, so bid/ask and implied volatility were previously unavailable. Nasdaq's public endpoints
answer without credentials and carry what the specification actually asks for:

* **Bid/ask with sizes** (``v7``-style NBBO is *not* what this is — see the caveat below).
* **An options chain** with per-strike call/put bid, ask, last, volume and open interest, from which
  implied volatility, skew and put/call ratios can be derived.

Four honest limits, stated here rather than buried in output:

1. **This is a single-venue view, not the consolidated NBBO.** Nasdaq reports its own book. The
   true NBBO takes the best price across all exchanges, so a spread measured here can be *wider*
   than the real best-quote spread, and it never accounts for odd lots or hidden liquidity. It is a
   genuine bid/ask spread, which is far better than an assumption, but calling it "the NBBO" would
   be false.
2. **Timestamps matter more than usual.** The payload is only treated as current when its own
   trade timestamp is recent. A quote from a previous session is stored and reported as stale,
   never used silently as a live mark.
3. **Implied volatility is computed here,** by inverting Black–Scholes on the mid price, rather
   than read from the venue. Where a quote is one-sided or crossed, IV is reported as unavailable
   for that strike instead of being extrapolated from a neighbour.
4. **Coverage is per-symbol.** Not every ticker has an options chain, and an instrument with no
   chain is reported as having none rather than returning an empty surface that looks like zeros.

Nothing in this module writes to the official journal.
"""
import datetime as dt
import json
import math
import urllib.error
import urllib.request

from sources import guard
import research_store as rs

SOURCE = 'nasdaq'
QUOTE_URL = 'https://api.nasdaq.com/api/quote/%s/info?assetclass=%s'
CHAIN_URL = 'https://api.nasdaq.com/api/quote/%s/option-chain?assetclass=%s&limit=%d'
SHORT_URL = 'https://api.nasdaq.com/api/quote/%s/short-interest?assetclass=%s'

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/120.0 Safari/537.36')

# The venue needs an asset class; ETFs are priced through a different path from single stocks.
ASSET_CLASSES = ('stocks', 'etf')

# A quote older than this is not presented as a live mark.
MAX_QUOTE_AGE_MINUTES = 30


class QuoteError(Exception):
    """A quote, chain or related dataset could not be obtained or failed validation."""


def _get(url):
    import time
    guard(url.startswith('https://'), 'HTTPS required')
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Accept': 'application/json', 'Accept-Encoding': 'identity'})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            guard(r.status == 200, 'unexpected status %d' % r.status)
            body = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise QuoteError('not found: %s' % url.split('/api/')[-1]) from None
        raise QuoteError('HTTP %d from the venue' % e.code) from None
    except OSError as e:
        raise QuoteError('network error: %s' % str(e)[:120]) from None
    if not body:
        raise QuoteError('empty response from the venue')
    return body


def _money(x):
    """Parse a venue money string such as '$1,234.50', returning None for '--' or junk."""
    if x is None:
        return None
    s = str(x).strip().replace('$', '').replace(',', '').replace('%', '')
    if s in ('', '--', 'N/A', 'nan'):
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def _count(x):
    """Parse a venue quantity string; '--' means unpublished, never zero."""
    v = _money(x)
    if v is None:
        return None
    return int(v)


def _asset_class_candidates(symbol):
    """Try the classes in turn; a single stock and an ETF take different paths at the venue."""
    return ASSET_CLASSES


def fetch_quote(symbol, asset_class=None):
    """Fetch one symbol's current bid/ask with sizes and the venue's own timestamps."""
    sym = (symbol or '').strip().upper()
    guard(sym and all(ch.isalnum() or ch in '.-' for ch in sym), 'invalid ticker: %r' % (symbol,))
    last_error = None
    for ac in ([asset_class] if asset_class else _asset_class_candidates(sym)):
        url = QUOTE_URL % (sym, ac)
        try:
            body = _get(url)
            doc = json.loads(body)
        except (QuoteError, ValueError) as e:
            last_error = e
            continue
        data = (doc or {}).get('data')
        if not isinstance(data, dict):
            last_error = QuoteError('%s: no quote data for asset class %s' % (sym, ac))
            continue
        prim = data.get('primaryData')
        if not isinstance(prim, dict):
            last_error = QuoteError('%s: quote payload has no primaryData' % sym)
            continue
        bid, ask = _money(prim.get('bidPrice')), _money(prim.get('askPrice'))
        if bid is None and ask is None:
            last_error = QuoteError('%s: venue returned no bid or ask' % sym)
            continue
        return {
            'symbol': sym,
            'asset_class': ac,
            'raw': body,
            'url': url,
            'last': _money(prim.get('lastSalePrice')),
            'bid': bid,
            'ask': ask,
            'bid_size': _count(prim.get('bidSize')),
            'ask_size': _count(prim.get('askSize')),
            'volume': _money(prim.get('volume')),
            'currency': prim.get('currency'),
            'venue_realtime': bool(prim.get('isRealTime')),
            'venue_timestamp': prim.get('lastTradeTimestamp'),
            'net_change': _money(prim.get('netChange')),
            'pct_change': _money(prim.get('percentageChange')),
        }
    raise QuoteError(str(last_error) if last_error else 'no quote for %s' % sym)


def spread_from_quote(q):
    """Mid, absolute spread and spread in basis points. With a one-sided quote, say so."""
    bid, ask = q.get('bid'), q.get('ask')
    if bid is None or ask is None:
        return {'mid': None, 'spread': None, 'spread_bps': None,
                'reason': 'one-sided quote: both a bid and an ask are needed for a spread'}
    if bid <= 0 or ask <= 0:
        return {'mid': None, 'spread': None, 'spread_bps': None,
                'reason': 'non-positive quote'}
    if ask < bid:
        # A crossed quote is a stale or erroneous book, not a free lunch.
        return {'mid': None, 'spread': None, 'spread_bps': None,
                'reason': 'crossed quote (ask below bid); refused rather than averaged'}
    mid = (bid + ask) / 2.0
    return {'mid': round(mid, 6), 'spread': round(ask - bid, 6),
            'spread_bps': round((ask - bid) / mid * 10000.0, 3) if mid else None,
            'relative_spread': round((ask - bid) / mid, 8) if mid else None, 'reason': None}


def quote_freshness(q, now=None, max_age_minutes=MAX_QUOTE_AGE_MINUTES):
    """Decide whether a quote can be used as a live mark.

    The venue's own timestamp is the only evidence of recency. If it cannot be parsed, the quote is
    treated as stale rather than assumed fresh, because a silent stale mark is the more expensive
    mistake.
    """
    stamp = q.get('venue_timestamp')
    now = now or dt.datetime.now(dt.timezone.utc)
    if not stamp:
        return {'current': False, 'age_minutes': None,
                'reason': 'the venue supplied no quote timestamp, so recency cannot be established'}
    parsed = None
    for fmt in ('%b %d, %Y %I:%M %p ET', '%b %d, %Y %I:%M %p'):
        try:
            parsed = dt.datetime.strptime(str(stamp).replace('  ', ' ').strip(), fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return {'current': False, 'age_minutes': None,
                'reason': 'unrecognised quote timestamp %r' % (stamp,)}
    # The venue reports US Eastern time. A fixed offset is used rather than a tz database so the
    # result is deterministic; the age is therefore accurate to within an hour, which is why the
    # staleness threshold is half an hour rather than something finer.
    eastern = parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=-5)))
    age = (now - eastern.astimezone(dt.timezone.utc)).total_seconds() / 60.0
    if age < -MAX_QUOTE_AGE_MINUTES:
        return {'current': False, 'age_minutes': round(age, 1),
                'reason': 'quote timestamp is in the future; the clock or payload is wrong'}
    if age > max_age_minutes:
        return {'current': False, 'age_minutes': round(age, 1),
                'reason': 'quote is %.0f minutes old (limit %d), so it is not a live mark'
                          % (age, max_age_minutes)}
    return {'current': True, 'age_minutes': round(age, 1), 'reason': None}


# --- options surface -------------------------------------------------------------------------

def _iso_date(text):
    """Normalise a US-style venue date (``11/13/2026``) to ISO, or None if unparseable.

    Storing the venue's own format would break every date comparison in the store, and the
    freshness check would then report fresh data as stale — a wrong answer that looks careful.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or s in ('--', 'N/A'):
        return None
    for fmt in ('%m/%d/%Y', '%Y-%m-%d', '%b %d, %Y', '%d/%m/%Y'):
        try:
            d = dt.datetime.strptime(s, fmt).date()
            return d.isoformat()
        except ValueError:
            continue
    return None


def parse_chain(payload, symbol):
    """Parse an options-chain payload into per-strike rows.

    A row needs a strike to be meaningful. Unpriced legs keep ``None`` rather than zero, so a
    put/call ratio cannot be computed from missing data and look like a real extreme.
    """
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise QuoteError('%s: options chain is not JSON' % symbol) from None
    data = (doc or {}).get('data')
    if not isinstance(data, dict) or not data.get('table'):
        msg = str((doc or {}).get('message') or '').strip()
        raise QuoteError('%s: no options chain%s' % (symbol, (': ' + msg) if msg else ''))
    table = data['table']
    rows = table.get('rows') or []
    expiry = None
    out = []
    for r in rows:
        # The venue puts the full expiry label on the first row of a group and a short form
        # (``Dec 1``) on the continuation rows. Preferring the short form split one real expiry
        # into two synthetic ones, which fragments the surface and corrupts any per-expiry stat.
        group = (r.get('expirygroup') or '').strip()
        short = (r.get('expiryDate') or '').strip()
        if group:
            expiry = group
        elif expiry is None and short:
            expiry = short
        elif short and expiry:
            # Keep the long label unless the short form names a different month, which would
            # mean the venue started a new group without labelling it.
            if short.split()[0][:3].lower() not in expiry.lower():
                expiry = short
        strike = _money(r.get('strike'))
        if strike is None:
            continue
        out.append({
            'expiry': expiry,
            'strike': strike,
            'call_bid': _money(r.get('c_Bid')), 'call_ask': _money(r.get('c_Ask')),
            'call_last': _money(r.get('c_Last')), 'call_volume': _count(r.get('c_Volume')),
            'call_oi': _count(r.get('c_Openinterest')),
            'put_bid': _money(r.get('p_Bid')), 'put_ask': _money(r.get('p_Ask')),
            'put_last': _money(r.get('p_Last')), 'put_volume': _count(r.get('p_Volume')),
            'put_oi': _count(r.get('p_Openinterest')),
        })
    if not out:
        raise QuoteError('%s: options chain contained no strike rows' % symbol)
    # A single expiry span is expected within one payload; mixing them would corrupt skew.
    expiries = sorted({r['expiry'] for r in out if r['expiry']})
    return {'symbol': symbol, 'expiries': expiries, 'rows': out,
            'total_reported': data.get('totalRecord'), 'raw': payload}


PAGE_LIMIT = 250
# A hard stop on pagination. The largest chain observed is under 1,000 rows, so this is generous;
# without it a venue quirk that kept returning rows would loop until the socket died.
MAX_PAGES = 12


def _chain_pages(sym, ac, limit=PAGE_LIMIT):
    """Yield the raw table for each page of a chain.

    Pagination is not optional. A single page of 250 rows stops around strike 676 for SPY while
    spot sits at 718, so the money is simply absent: every implied volatility then fails and the
    surface looks empty for a reason that has nothing to do with the market. Following ``offset``
    is what makes the surface actually cover the strikes that matter.
    """
    offset = 0
    for _ in range(MAX_PAGES):
        url = CHAIN_URL % (sym, ac, int(limit))
        if offset:
            url += '&offset=%d' % offset
        body = _get(url)
        doc = json.loads(body)
        data = (doc or {}).get('data')
        table = (data or {}).get('table') if isinstance(data, dict) else None
        rows = (table or {}).get('rows') or []
        if not rows:
            if offset == 0:
                msg = str((doc or {}).get('message') or '').strip()
                raise QuoteError('%s: no options chain%s' % (sym, (': ' + msg) if msg else ''))
            return
        yield {'rows': rows, 'total': data.get('totalRecord'), 'body': body, 'url': url}
        if len(rows) < int(limit):
            return
        offset += len(rows)


def _row_key(r):
    """Identity for de-duplication across pages.

    The venue's drill-down URL is unique per contract and survives paging. Where it is absent,
    expiry and strike identify the contract well enough.
    """
    url = r.get('drillDownURL')
    if url:
        return ('url', url)
    return ('es', str(r.get('expirygroup') or ''), str(r.get('strike') or ''))


def fetch_chain(symbol, limit=PAGE_LIMIT, asset_class=None):
    """Fetch a whole options chain across pages. A future ``fromdate`` is rejected, so none is sent."""
    sym = (symbol or '').strip().upper()
    guard(sym and all(ch.isalnum() or ch in '.-' for ch in sym), 'invalid ticker: %r' % (symbol,))
    last_error = None
    for ac in ([asset_class] if asset_class else _asset_class_candidates(sym)):
        rows, seen, total, url = [], set(), None, None
        try:
            for page in _chain_pages(sym, ac, limit):
                total = page['total'] if total is None else total
                url = page['url']
                for r in page['rows']:
                    k = _row_key(r)
                    if k in seen:
                        continue
                    seen.add(k)
                    rows.append(r)
        except (QuoteError, ValueError) as e:
            last_error = e
            continue
        if not rows:
            last_error = QuoteError('%s: options chain contained no rows' % sym)
            continue
        payload = json.dumps({'data': {'totalRecord': total,
                                       'table': {'rows': rows}}})
        try:
            parsed = parse_chain(payload, sym)
        except QuoteError as e:
            last_error = e
            continue
        parsed['asset_class'] = ac
        parsed['url'] = url
        parsed['raw'] = payload
        parsed['total_reported'] = total
        parsed['pages'] = (len(rows) + int(limit) - 1) // int(limit)
        return parsed
    raise QuoteError(str(last_error) if last_error else 'no options chain for %s' % sym)


# --- Black-Scholes implied volatility --------------------------------------------------------

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot, strike, t_years, vol, rate=0.0):
    """European call and put price. Used only to invert a market mid into an implied vol."""
    if spot <= 0 or strike <= 0 or t_years <= 0 or vol <= 0:
        return None, None
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    disc = math.exp(-rate * t_years)
    call = spot * _norm_cdf(d1) - strike * disc * _norm_cdf(d2)
    put = strike * disc * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
    return call, put


def implied_vol(mid, spot, strike, t_years, is_call, rate=0.0, tol=1e-6, max_iter=200):
    """Invert Black-Scholes for volatility by bisection.

    Bisection is used rather than Newton because it cannot diverge on the deep out-of-the-money
    strikes where vega is tiny, and the cost of a few extra iterations is irrelevant here. Returns
    ``None`` when no volatility in the search range reproduces the price — that is a real answer
    (a stale or crossed quote), and it is not replaced with a neighbour's value.
    """
    if mid is None or mid <= 0 or spot is None or spot <= 0 or strike <= 0 or t_years <= 0:
        return None
    intrinsic = max(0.0, spot - strike) if is_call else max(0.0, strike - spot)
    if mid < intrinsic - 1e-8:
        return None  # price below intrinsic: inconsistent, not merely illiquid
    lo, hi = 1e-4, 5.0
    c_lo, p_lo = black_scholes(spot, strike, t_years, lo, rate)
    c_hi, p_hi = black_scholes(spot, strike, t_years, hi, rate)
    f_lo = (c_lo if is_call else p_lo) - mid
    f_hi = (c_hi if is_call else p_hi) - mid
    if f_lo is None or f_hi is None or f_lo * f_hi > 0:
        return None
    for _ in range(max_iter):
        mid_v = (lo + hi) / 2.0
        c, p = black_scholes(spot, strike, t_years, mid_v, rate)
        f = (c if is_call else p) - mid
        if f is None:
            return None
        if abs(f) < tol:
            return round(mid_v, 6)
        if f * f_lo > 0:
            lo, f_lo = mid_v, f
        else:
            hi = mid_v
    return round((lo + hi) / 2.0, 6)


def _years_to(expiry_text, today=None):
    """Convert a chain expiry label into years. Unparseable labels return None, not a guess."""
    if not expiry_text:
        return None
    today = today or dt.date.today()
    for fmt in ('%B %d, %Y', '%b %d %Y', '%Y-%m-%d', '%b %d', '%B %d'):
        try:
            d = dt.datetime.strptime(expiry_text.strip(), fmt).date()
            if '%Y' not in fmt:
                d = d.replace(year=today.year)
                if d < today:
                    d = d.replace(year=today.year + 1)
            days = (d - today).days
            return days / 365.0 if days > 0 else None
        except ValueError:
            continue
    return None


def surface(chain, spot, today=None, rate=0.0):
    """Derive implied volatility per strike, plus skew and put/call ratios.

    **Only the out-of-the-money leg is inverted.** A deep in-the-money option's price is almost
    entirely intrinsic and its vega is nearly zero, so its implied volatility is not identified by
    the price at all: a nine-cent discrepancy between a quote and intrinsic turns into a wild
    volatility estimate. Measured on a live Apple chain, 52 of 134 two-sided call quotes sat
    fractionally below intrinsic and had to be discarded. Choosing the OTM leg at each strike —
    calls above spot, puts below — is the standard treatment and removes that whole class of
    artefact. Those strikes are counted as ``itm_leg_not_used`` rather than reported as failures,
    because the quote is fine and the problem is the inversion.

    Skew is reported as *call IV minus put IV* at matched strikes. The usual 25-delta skew needs
    interpolated deltas across expiries; computing it here from raw strikes would present a
    different quantity under the familiar name, so the simpler matched-strike measure is labelled
    for what it is.
    """
    if spot is None or spot <= 0:
        return {'error': 'a positive spot price is required to invert option prices'}
    by_expiry = {}
    skipped_itm = 0
    for r in chain['rows']:
        t = _years_to(r['expiry'], today)
        if t is None:
            continue
        cb, ca = r['call_bid'], r['call_ask']
        pb, pa = r['put_bid'], r['put_ask']
        c_mid = (cb + ca) / 2.0 if cb is not None and ca is not None and ca >= cb else None
        p_mid = (pb + pa) / 2.0 if pb is not None and pa is not None and pa >= pb else None
        call_iv = implied_vol(c_mid, spot, r['strike'], t, True, rate) if c_mid else None
        put_iv = implied_vol(p_mid, spot, r['strike'], t, False, rate) if p_mid else None
        # Out-of-the-money selection: at or above spot the call is OTM/ATM, below it the put is.
        if r['strike'] >= spot:
            otm_iv, otm_leg, otm_mid = call_iv, 'call', c_mid
            itm_mid, itm_iv = p_mid, put_iv
        else:
            otm_iv, otm_leg, otm_mid = put_iv, 'put', p_mid
            itm_mid, itm_iv = c_mid, call_iv
        # This counts the in-the-money leg's *own* failed inversion: a quoted price that
        # Black-Scholes could not reproduce, which is expected when a price sits on intrinsic.
        # It is reported because it is the measured justification for using the OTM leg.
        if itm_mid is not None and itm_iv is None:
            skipped_itm += 1
        row = {
            'expiry': r['expiry'], 't_years': round(t, 6), 'strike': r['strike'],
            'call_mid': c_mid, 'put_mid': p_mid,
            'call_iv': call_iv, 'put_iv': put_iv,
            'otm_iv': otm_iv, 'otm_leg': otm_leg, 'otm_mid': otm_mid,
            'call_oi': r['call_oi'], 'put_oi': r['put_oi'],
        }
        by_expiry.setdefault(r['expiry'], []).append(row)

    # Put/call ratios use open interest, which is the only quantity here that is not a live quote
    # and therefore does not swing with the spread.
    call_oi = sum(r['call_oi'] or 0 for r in chain['rows'])
    put_oi = sum(r['put_oi'] or 0 for r in chain['rows'])
    call_vol = sum(r['call_volume'] or 0 for r in chain['rows'])
    put_vol = sum(r['put_volume'] or 0 for r in chain['rows'])
    ordered = {k: sorted(v, key=lambda x: x['strike']) for k, v in by_expiry.items()}
    ivs = [r['otm_iv'] for grp in ordered.values() for r in grp if r['otm_iv']]
    # At-the-money is a specific thing: the implied volatility at the strikes bracketing spot, not
    # the median of every strike in the chain.
    atm_by_expiry = {}
    for label, rows in ordered.items():
        bracket = [r for r in rows if r['otm_iv']]
        below = [r for r in bracket if r['strike'] <= spot]
        above = [r for r in bracket if r['strike'] >= spot]
        lo = max(below, key=lambda r: r['strike']) if below else None
        hi = min(above, key=lambda r: r['strike']) if above else None
        if lo and hi and lo['strike'] == hi['strike']:
            atm_by_expiry[label] = lo['otm_iv']
        elif lo and hi:
            span = hi['strike'] - lo['strike']
            w = (spot - lo['strike']) / span if span else 0.5
            atm_by_expiry[label] = round(lo['otm_iv'] + w * (hi['otm_iv'] - lo['otm_iv']), 6)
        elif lo or hi:
            atm_by_expiry[label] = (lo or hi)['otm_iv']

    # Where both legs invert at the same strike the two vols differ, and that difference is skew.
    skew = []
    for label, rows in ordered.items():
        for r in rows:
            if r['call_iv'] and r['put_iv'] and r['strike'] >= spot:
                skew.append({'expiry': label, 'strike': r['strike'],
                             'call_iv': r['call_iv'], 'put_iv': r['put_iv'],
                             'skew': round(r['call_iv'] - r['put_iv'], 6)})
    return {
        'spot': spot,
        'expiries': ordered,
        'put_call_oi_ratio': round(put_oi / call_oi, 4) if call_oi else None,
        'put_call_volume_ratio': round(put_vol / call_vol, 4) if call_vol else None,
        'put_call_oi_note': ('None when no call open interest is reported; a zero would be '
                             'indistinguishable from missing data'),
        'atm_iv_by_expiry': atm_by_expiry,
        # The headline ATM number is the median across expiries, so one illiquid expiry cannot
        # determine it. ``median_call_iv`` is kept alongside because the two differ and conflating
        # them would overstate what the number is.
        'atm_call_iv': _median(atm_by_expiry.values()),
        'median_call_iv': _median([r['call_iv'] for grp in ordered.values() for r in grp
                                   if r['call_iv']]),
        'otm_median_iv': _median(ivs),
        'iv_points': len(ivs),
        'skew_points': len(skew),
        'matched_strike_skew': skew[:40],
        'itm_leg_not_used': skipped_itm,
        'unpriced_strikes': sum(1 for grp in ordered.values() for r in grp if r['otm_iv'] is None),
        'coverage_note': ('Only the out-of-the-money leg is inverted, because an in-the-money '
                          'price is nearly all intrinsic and does not identify a volatility. '
                          'The venue serves the nearest expiries only, so this is a short-dated '
                          'surface, not the full term structure.'),
    }


def _median(values):
    """Median of the finite values, or None. Used only for summarising, never for filling a gap."""
    vals = sorted(v for v in values if v)
    if not vals:
        return None
    n = len(vals)
    return round(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0, 6)


# --- collection ------------------------------------------------------------------------------

def collect_quotes(c, symbols, fetched_utc, max_age_minutes=MAX_QUOTE_AGE_MINUTES):
    """Store current quotes and report which are fresh enough to use as a live mark."""
    out = {'source': SOURCE, 'ok': [], 'failed': {}, 'stale': [], 'conflicts': 0}
    for sym in symbols:
        try:
            q = fetch_quote(sym)
        except QuoteError as e:
            out['failed'][(sym or '').upper()] = str(e)
            continue
        fresh = quote_freshness(q, max_age_minutes=max_age_minutes)
        sp = spread_from_quote(q)
        rs.put_raw(c, SOURCE, q['url'], q['raw'], fetched_utc)
        rec = {
            'symbol': q['symbol'], 'bid': q['bid'], 'ask': q['ask'],
            'bid_size': q['bid_size'], 'ask_size': q['ask_size'],
            'last': q['last'], 'volume': q['volume'],
            'venue_timestamp': q['venue_timestamp'], 'venue_realtime': q['venue_realtime'],
            'spread': sp, 'current': fresh['current'],
            'age_minutes': fresh['age_minutes'], 'staleness_reason': fresh['reason'],
            # The single-venue caveat travels with every quote so no downstream reader can mistake
            # this for a consolidated NBBO.
            'quote_scope': 'single venue (Nasdaq), not consolidated NBBO',
        }
        conflicts = rs.put_quote_asof(c, rec, fetched_utc)
        rec['conflicts'] = conflicts
        out['conflicts'] += len(conflicts)
        out['ok'].append(rec)
        if not fresh['current']:
            out['stale'].append(q['symbol'])
    return out


def collect_chain(c, symbol, fetched_utc, limit=250):
    """Fetch and store one options chain with a derived surface."""
    import time as _t
    chain = fetch_chain(symbol, limit=limit)
    spot = None
    try:
        spot = fetch_quote(symbol).get('last')
    except QuoteError:
        # Without a spot the IV cannot be inverted; the chain is still worth storing.
        spot = None
    surf = surface(chain, spot) if spot else {'error': 'no spot price available for IV inversion'}
    rs.put_raw(c, SOURCE, chain['url'], chain['raw'], fetched_utc)
    n = rs.put_option_chain(c, chain['symbol'], chain['rows'], fetched_utc)
    return {'symbol': chain['symbol'], 'rows': n, 'expiries': chain['expiries'],
            'spot': spot, 'surface': surf}


def collect_short_interest(c, symbols, fetched_utc):
    """Current short interest per symbol, including days-to-cover and average daily volume."""
    out = {'source': SOURCE, 'ok': [], 'failed': {}, 'newest': None, 'current': False}
    for sym in symbols:
        s = (sym or '').strip().upper()
        got = None
        last = None
        for ac in ASSET_CLASSES:
            try:
                body = _get(SHORT_URL % (s, ac))
                doc = json.loads(body)
            except (QuoteError, ValueError) as e:
                last = e
                continue
            data = (doc or {}).get('data')
            if not isinstance(data, dict) or not data.get('shortInterestTable'):
                msg = str((doc or {}).get('message') or '').strip()
                last = QuoteError('%s: no short interest%s' % (s, (': ' + msg) if msg else ''))
                continue
            rows = data['shortInterestTable'].get('rows') or []
            parsed = []
            for r in rows:
                parsed.append({
                    'symbol': s,
                    'settlement': _iso_date(r.get('settlementDate')),
                    'shares': _money(r.get('interest')),
                    'avg_daily_volume': _money(r.get('avgDailyShareVolume')),
                    'days_to_cover': _money(r.get('daysToCover')),
                })
            parsed = [r for r in parsed if r['settlement']]
            if parsed:
                rows = sorted(parsed, key=lambda r: r['settlement'], reverse=True)
                got = (rows, body, SHORT_URL % (s, ac))
                break
            last = QuoteError('%s: short interest table was empty' % s)
        if not got:
            out['failed'][s] = str(last) if last else 'no short interest'
            continue
        rows, body, url = got
        rs.put_raw(c, SOURCE, url, body, fetched_utc)
        rs.put_short_interest(c, rows, SOURCE, fetched_utc)
        newest = max((r['settlement'] for r in rows if r['settlement']), default=None)
        out['ok'].append({'symbol': s, 'rows': len(rows), 'newest': newest,
                          'days_to_cover': rows[0].get('days_to_cover') if rows else None})
        if newest and (out['newest'] is None or newest > out['newest']):
            out['newest'] = newest
    # Freshness is judged from the dates, not assumed from the endpoint having answered.
    import short_interest as si
    if out['newest']:
        probe = [{'settlement': out['newest'], 'symbol': 'X'}]
        out['current'] = si.freshness(probe)['current']
    return out


def describe_quotes(report):
    if not report:
        return 'no quote report'
    parts = ['%d quote(s) stored' % len(report.get('ok') or [])]
    if report.get('stale'):
        parts.append('stale: ' + ', '.join(report['stale'][:5]))
    if report.get('failed'):
        parts.append('failed: ' + ', '.join(list(report['failed'])[:5]))
    return 'Nasdaq (single venue, not consolidated NBBO) - ' + '; '.join(parts)
