"""Yahoo Finance daily prices (unofficial endpoint, personal use only — see the license register).

Each symbol becomes three series:

* ``yahoo:SYM:close``    — the raw session close. This is the execution reference. A completed
  session's close is final, so it is point in time from the end of its trading day
  (``vintages=True``; ``known_at`` = session end, capped at retrieval time).
* ``yahoo:SYM:adjclose`` — dividend/split-adjusted close for total-return maths. Its *whole
  history is rewritten* at every distribution, so it is only point in time from retrieval
  (``vintages=False``). Never use it as an execution price.
* ``yahoo:SYM:volume``   — shares traded, for liquidity limits.

A bar fetched while its session is still open is stored as ``provisional`` with a note: it is
the last trade, not a close.
"""
import datetime as dt
import json
from urllib.parse import quote

from .base import Adapter, AdapterError, SeriesMeta, Observation, iso, utc_now
from ..contract import clean_value, parse_time
from asset_classes import asset_class

CHART = 'https://query1.finance.yahoo.com/v8/finance/chart/%s?range=%s&interval=1d&events=div%%2Csplits'

DEFAULT_UNIVERSE = ('SPY', 'QQQ', 'IWM', 'EFA', 'EEM', 'TLT', 'IEF', 'SHY', 'BIL', 'TIP', 'LQD',
                    'HYG', 'GLD', 'SLV', 'DBC', 'USO', 'UUP')

FIELDS = ('close', 'adjclose', 'volume')


def _category(sym):
    cls = asset_class(sym) or ''
    if cls in ('cash', 'short_bond', 'bond', 'long_bond'):
        return 'rates'
    if cls == 'credit':
        return 'credit'
    if cls in ('gold', 'commodity'):
        return 'commodities'
    return 'markets'


def series_for(symbol, currency='USD'):
    sym = symbol.upper()
    cat = _category(sym)
    return [
        SeriesMeta(series_id='yahoo:%s:close' % sym, source='yahoo', name='%s close' % sym,
                   kind='price', category=cat, unit=currency, frequency='D', license_id='yahoo',
                   currency=currency, native_id=sym, vintages=True,
                   notes='raw session close; execution reference'),
        SeriesMeta(series_id='yahoo:%s:adjclose' % sym, source='yahoo', name='%s adjusted close' % sym,
                   kind='price', category=cat, unit=currency, frequency='D', license_id='yahoo',
                   currency=currency, native_id=sym, vintages=False,
                   notes='total-return adjusted; history rewritten at distributions; not an execution price'),
        SeriesMeta(series_id='yahoo:%s:volume' % sym, source='yahoo', name='%s volume' % sym,
                   kind='other', category=cat, unit='shares', frequency='D', license_id='yahoo',
                   native_id=sym, vintages=True, notes='session volume'),
    ]


class YahooAdapter(Adapter):
    source = 'yahoo'
    license_id = 'yahoo'
    hosts = ('query1.finance.yahoo.com',)

    def __init__(self, symbols=None, range_='2y'):
        self.symbols = tuple(s.upper() for s in (symbols or DEFAULT_UNIVERSE))
        self.range = range_

    def catalog(self):
        return [m for s in self.symbols for m in series_for(s)]

    def fetch(self, series_ids=None, now=None):
        wanted = self.symbols
        if series_ids:
            wanted = tuple(dict.fromkeys(m.native_id for m in self.ids(series_ids)))
        out = []
        for sym in wanted:
            if not all(ch.isalnum() or ch in '._-^=' for ch in sym):
                raise AdapterError('invalid ticker: %r' % sym)
            body = self.get(CHART % (quote(sym, safe=''), self.range))
            out.extend(parse(body, sym, iso(now or utc_now())))
        return out


def parse(payload, symbol, retrieved_at):
    """Yahoo chart JSON -> close / adjclose / volume observations. Pure."""
    sym = symbol.upper()
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise AdapterError('yahoo %s: response is not JSON' % sym) from None
    chart = (doc or {}).get('chart') or {}
    if chart.get('error'):
        raise AdapterError('yahoo %s: %s' % (sym, str(chart['error'])[:200]))
    res = (chart.get('result') or [None])[0]
    if not res:
        raise AdapterError('yahoo %s: no data' % sym)
    meta = res.get('meta') or {}
    if meta.get('currency') and meta['currency'] != 'USD':
        raise AdapterError('yahoo %s: currency %s; only USD instruments are supported so far'
                           % (sym, meta['currency']))
    stamps = res.get('timestamp') or []
    ind = res.get('indicators') or {}
    quote_ = ((ind.get('quote') or [{}])[0]) or {}
    closes = quote_.get('close') or []
    volumes = quote_.get('volume') or []
    adj = (((ind.get('adjclose') or [{}])[0]) or {}).get('adjclose') or []
    offset = int(meta.get('gmtoffset') or 0)
    regular = ((meta.get('currentTradingPeriod') or {}).get('regular') or {})
    live_start, live_end = regular.get('start'), regular.get('end')
    now_t = parse_time(retrieved_at)
    out = []
    for i, ts in enumerate(stamps):
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        day = (dt.datetime.fromtimestamp(ts + offset, dt.timezone.utc)).date().isoformat()
        in_session = bool(live_start and live_end and int(live_start) <= ts < int(live_end)
                          and now_t.timestamp() < int(live_end))
        session_end = iso(dt.datetime.fromtimestamp(int(live_end), dt.timezone.utc)) if (
            in_session) else iso(dt.datetime.fromisoformat(day).replace(
                hour=23, minute=59, second=59, tzinfo=dt.timezone.utc) - dt.timedelta(seconds=offset))
        known = min(session_end, retrieved_at)
        q_raw = 'provisional' if in_session else 'ok'
        note = 'intraday last trade, session still open' if in_session else ''
        vals = {'close': clean_value(closes[i] if i < len(closes) else None),
                'adjclose': clean_value(adj[i] if i < len(adj) else None),
                'volume': clean_value(volumes[i] if i < len(volumes) else None)}
        for f in FIELDS:
            v = vals[f]
            if v is None:
                # A non-trading gap is skipped, never filled; a missing close inside a returned
                # bar is recorded as missing so it is visible.
                if f == 'close' and closes:
                    out.append(Observation(series_id='yahoo:%s:%s' % (sym, f), period=day, value=None,
                                           known_at=known, retrieved_at=retrieved_at,
                                           vintage='yahoo:' + retrieved_at, quality='missing'))
                continue
            if f == 'adjclose':
                k, q = retrieved_at, q_raw
            else:
                k, q = known, q_raw
            out.append(Observation(series_id='yahoo:%s:%s' % (sym, f), period=day, value=v,
                                   known_at=k, retrieved_at=retrieved_at,
                                   vintage='yahoo:' + retrieved_at, quality=q, note=note))
    return out
