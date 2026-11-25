"""Daily price history from Yahoo Finance, for paper trading only.

Yahoo's chart endpoint is a convenient *unofficial* service: undocumented, unversioned and free
to change or rate-limit without notice. It is therefore kept strictly outside the collected
journal. Nothing fetched here is written to the SQLite record, no official feed depends on it,
and the paper account labels its prices with ``source: yahoo`` so a simulated result is never
mistaken for one driven by a vetted publisher.

Two data decisions matter more than the transport:

* **Adjusted close is used, not raw close.** Raw closes on this endpoint are split-adjusted but
  not dividend-adjusted, so a 3% dividend would appear as a 3% price fall. ``adjclose`` captures
  both, which is what a total-return paper account needs.
* **Every series is validated before it is used.** Timestamps must be unique and increasing,
  prices must be positive and finite, and a short or gappy series is rejected rather than padded.
  A rejected symbol raises with its reason; it is never silently treated as flat.
"""
import json, math, os, tempfile, time
from pathlib import Path
from urllib.parse import quote

from net import fetch, FetchFailure
from sources import guard

CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d&events=div%%2Csplits'
HOST = 'query1.finance.yahoo.com'
MIN_PRICES = 2
MIN_HISTORY_FOR_VOL = 10
CACHE_NAME = 'yahoo_prices.json'


class PriceError(Exception):
    """A price series could not be obtained or failed validation."""


def _parse_chart(symbol, payload):
    """Turn one chart response into a validated series. Pure, so it can be tested offline."""
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise PriceError('%s: response is not JSON' % symbol) from None
    chart = (doc or {}).get('chart') or {}
    if chart.get('error'):
        err = chart['error']
        raise PriceError('%s: %s' % (symbol, (err.get('description') or err.get('code') or 'rejected by the provider')))
    results = chart.get('result') or []
    if not results:
        raise PriceError('%s: no data returned' % symbol)
    res = results[0]
    stamps = res.get('timestamp') or []
    ind = res.get('indicators') or {}
    # Prefer adjusted close; fall back to close only if the provider omitted it.
    adj = ((ind.get('adjclose') or [{}])[0] or {}).get('adjclose')
    raw = ((ind.get('quote') or [{}])[0] or {}).get('close')
    values = adj if adj else raw
    if not values or not stamps:
        raise PriceError('%s: no close prices returned' % symbol)
    if len(values) != len(stamps):
        raise PriceError('%s: %d prices for %d timestamps' % (symbol, len(values), len(stamps)))
    dates, prices, seen = [], [], set()
    for t, v in zip(stamps, values):
        if v is None:  # a genuine non-trading gap is skipped, never filled with a guess
            continue
        try:
            px = float(v)
            stamp = int(t)
        except (TypeError, ValueError):
            raise PriceError('%s: unparseable price or timestamp' % symbol) from None
        if not math.isfinite(px) or px <= 0:
            raise PriceError('%s: non-positive or non-finite price' % symbol)
        if stamp in seen:
            raise PriceError('%s: duplicate timestamp' % symbol)
        if dates and stamp <= dates[-1]:
            raise PriceError('%s: timestamps are not increasing' % symbol)
        seen.add(stamp)
        dates.append(stamp)
        prices.append(px)
    if len(prices) < MIN_PRICES:
        raise PriceError('%s: only %d usable prices' % (symbol, len(prices)))
    meta = res.get('meta') or {}
    return {
        'symbol': str(meta.get('symbol') or symbol).upper(),
        'prices': prices,
        'dates': dates,
        'currency': meta.get('currency'),
        'exchange': meta.get('exchangeName'),
        'adjusted': bool(adj),
        'first_utc': dates[0],
        'last_utc': dates[-1],
    }


def fetch_symbol(symbol, rng='2y'):
    """Fetch and validate one symbol's daily history. Raises ``PriceError`` on any problem."""
    sym = (symbol or '').strip().upper()
    if not sym:
        raise PriceError('empty symbol')
    guard(all(c.isalnum() or c in '._-^=' for c in sym), 'invalid ticker: ' + sym)
    url = CHART % (quote(sym, safe=''), rng)
    try:
        body, final_url, _, _ = fetch(url)
    except FetchFailure as e:
        raise PriceError('%s: %s' % (sym, e)) from None
    if final_url.startswith('https://') and HOST not in final_url:
        raise PriceError('%s: unexpected host in response' % sym)
    return _parse_chart(sym, body)


def _cache_read(path):
    try:
        doc = json.loads(Path(path).read_text('utf-8'))
    except (OSError, ValueError, TypeError):
        return {}
    series = doc.get('series') if isinstance(doc, dict) else None
    return series if isinstance(series, dict) else {}


def _cache_write(path, series):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile('w', dir=p.parent, prefix='prices-', suffix='.tmp',
                                         encoding='utf-8', delete=False) as f:
            tmp = Path(f.name)
            json.dump({'source': 'yahoo', 'written_utc': time.time(), 'series': series},
                      f, default=str)
        os.replace(tmp, p)
        tmp = None
        return True
    except OSError:
        return False
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def fetch_prices(symbols, cache_path=None, max_age_hours=12.0, allow_network=True, rng='2y'):
    """Return ``{symbol: series}`` plus a report of what was used, cached or refused.

    The cache is consulted first. A fresh entry is reused without touching the network, so
    repeated paper runs are cheap and reproducible. If the network is unavailable and a stale
    cache entry exists it is used, but it is reported as ``stale`` rather than passed off as
    current.
    """
    wanted = []
    for s in symbols:
        sym = (s or '').strip().upper()
        if sym and sym not in wanted:
            wanted.append(sym)
    if not wanted:
        raise PriceError('no symbols requested')
    cache = _cache_read(cache_path) if cache_path else {}
    out, report = {}, {'source': 'yahoo', 'used': [], 'cached': [], 'stale': [],
                       'failed': {}, 'unofficial': True}
    now = time.time()
    changed = False
    for sym in wanted:
        entry = cache.get(sym)
        fresh = False
        if isinstance(entry, dict):
            try:
                age = now - float(entry.get('fetched_utc') or 0)
            except (TypeError, ValueError):
                age = None
            prices = entry.get('prices') or []
            if age is not None and age <= max_age_hours * 3600 and len(prices) >= MIN_PRICES:
                fresh = True
        if fresh:
            out[sym] = entry
            report['used'].append(sym)
            report['cached'].append(sym)
            continue
        if not allow_network:
            if isinstance(entry, dict) and (entry.get('prices') or []):
                out[sym] = entry
                report['used'].append(sym)
                report['stale'].append(sym)
            else:
                report['failed'][sym] = 'network disabled and no cached series'
            continue
        try:
            series = fetch_symbol(sym, rng=rng)
        except PriceError as e:
            # Fall back to a stale cache entry only if one exists, and say that it is stale.
            if isinstance(entry, dict) and len(entry.get('prices') or []) >= MIN_PRICES:
                out[sym] = entry
                report['used'].append(sym)
                report['stale'].append(sym)
                report['failed'][sym] = str(e) + ' (using cached series)'
            else:
                report['failed'][sym] = str(e)
            continue
        series['fetched_utc'] = now
        out[sym] = series
        cache[sym] = {k: series[k] for k in ('symbol', 'prices', 'dates', 'currency', 'exchange',
                                             'adjusted', 'fetched_utc', 'first_utc', 'last_utc')}
        report['used'].append(sym)
        changed = True
    if changed and cache_path:
        _cache_write(cache_path, cache)
    if not out:
        # Returning an empty map would let a caller proceed with no prices at all. Refuse here,
        # with the per-symbol reasons, so the failure is reported where it happened.
        detail = '; '.join('%s: %s' % (k, v) for k, v in report['failed'].items())
        raise PriceError(detail or 'no usable price series')
    return out, report


def price_series(series_map):
    """Reduce fetched series to ``{symbol: [prices]}``, the shape ``paper_trader`` expects."""
    return {sym: list(entry.get('prices') or []) for sym, entry in (series_map or {}).items()
            if len(entry.get('prices') or []) >= MIN_PRICES}


def realized_vols_from(series_map, window=20, long_window=250, halflife=30):
    """Annualised volatility per symbol from the fetched history.

    Uses ``asset_classes.blended_vol``: the larger of a short-window estimate and a long EWMA
    estimate, so the size reacts quickly to a shock but does not balloon on a brief calm patch.
    Too little history is refused rather than guessed.
    """
    from asset_classes import blended_vol
    vols, skipped = {}, {}
    for sym, entry in (series_map or {}).items():
        px = [float(p) for p in (entry.get('prices') or []) if p]
        if len(px) < MIN_HISTORY_FOR_VOL:
            skipped[sym] = 'only %d prices' % len(px)
            continue
        sd = blended_vol(px, short_window=window, long_window=long_window, halflife=halflife)
        if sd is None:
            skipped[sym] = 'zero variance'
            continue
        vols[sym] = round(sd, 6)
    return vols, skipped


def describe(report):
    """One-line, honest summary of where the prices came from."""
    if not report:
        return 'no price report'
    parts = []
    if report.get('cached'):
        parts.append('cached: ' + ', '.join(report['cached']))
    if report.get('stale'):
        parts.append('STALE: ' + ', '.join(report['stale']))
    if report.get('failed'):
        parts.append('failed: ' + ', '.join(report['failed']))
    body = '; '.join(parts) if parts else 'all live'
    return 'Yahoo Finance (unofficial), %d symbols — %s' % (len(report.get('used') or []), body)
