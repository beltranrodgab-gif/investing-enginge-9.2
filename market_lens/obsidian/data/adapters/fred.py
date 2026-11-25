"""FRED / ALFRED adapter (Federal Reserve Bank of St. Louis). Needs a free key in FRED_API_KEY.

Monthly, quarterly and annual series are loaded with **all historical vintages** from ALFRED
(``realtime_start=1776-07-04``), so a backtest sees each value only from the day it was first
published and sees revisions on the day they happened. ``known_at`` is the end of the
``realtime_start`` day (UTC): releases come out during the US day, so that is conservative.

Daily and weekly series are loaded as the current vintage (``known_at = retrieved_at``): their
full vintage history is very large and they are rarely revised. Their history is therefore only
point-in-time from the date this system first retrieved it, and the series notes say so.
"""
import json
from urllib.parse import urlencode

from .base import Adapter, AdapterError, SeriesMeta, Observation, iso, utc_now, period_end_from_start
from ..contract import clean_value, end_of_day

API = 'https://api.stlouisfed.org/fred/series/observations'
OBS_START = '1990-01-01'

# native id, name, category, kind, unit, frequency
CATALOG = (
    ('GDPC1', 'US real GDP', 'growth', 'macro', 'bn chained 2017 USD, SAAR', 'Q'),
    ('INDPRO', 'US industrial production', 'growth', 'macro', 'index 2017=100', 'M'),
    ('RSAFS', 'US retail sales', 'consumer', 'macro', 'mn USD, SA', 'M'),
    ('UMCSENT', 'UMich consumer sentiment', 'consumer', 'macro', 'index 1966Q1=100', 'M'),
    ('CPIAUCSL', 'US CPI all items', 'inflation', 'macro', 'index 1982-84=100, SA', 'M'),
    ('CPILFESL', 'US core CPI', 'inflation', 'macro', 'index 1982-84=100, SA', 'M'),
    ('PCEPI', 'US PCE price index', 'inflation', 'macro', 'index 2017=100, SA', 'M'),
    ('PCEPILFE', 'US core PCE price index', 'inflation', 'macro', 'index 2017=100, SA', 'M'),
    ('PPIACO', 'US PPI all commodities', 'inflation', 'macro', 'index 1982=100', 'M'),
    ('CES0500000003', 'US average hourly earnings', 'employment', 'macro', 'USD per hour, SA', 'M'),
    ('PAYEMS', 'US nonfarm payrolls', 'employment', 'macro', 'thousands, SA', 'M'),
    ('UNRATE', 'US unemployment rate', 'employment', 'macro', 'percent, SA', 'M'),
    ('CIVPART', 'US labour force participation', 'employment', 'macro', 'percent, SA', 'M'),
    ('OPHNFB', 'US nonfarm productivity', 'growth', 'macro', 'index 2017=100, SA', 'Q'),
    ('HOUST', 'US housing starts', 'housing', 'macro', 'thousands, SAAR', 'M'),
    ('PCE', 'US personal consumption expenditures', 'consumer', 'macro', 'bn USD, SAAR', 'M'),
    ('PSAVERT', 'US personal saving rate', 'consumer', 'macro', 'percent, SA', 'M'),
    ('M2SL', 'US M2 money stock', 'liquidity', 'macro', 'bn USD, SA', 'M'),
    ('GFDEGDQ188S', 'US federal debt to GDP', 'debt', 'fiscal', 'percent of GDP', 'Q'),
    ('FYFSGDA188S', 'US federal surplus/deficit to GDP', 'fiscal', 'fiscal', 'percent of GDP', 'A'),
    # daily / weekly: current vintage only
    ('DFF', 'Effective federal funds rate', 'central_bank', 'rate', 'percent', 'D'),
    ('DGS2', 'US Treasury 2y yield', 'rates', 'rate', 'percent', 'D'),
    ('DGS10', 'US Treasury 10y yield', 'rates', 'rate', 'percent', 'D'),
    ('T10Y2Y', 'US 10y minus 2y', 'rates', 'rate', 'percentage points', 'D'),
    ('DFII10', 'US 10y real yield (TIPS)', 'rates', 'rate', 'percent', 'D'),
    ('T10YIE', 'US 10y breakeven inflation', 'inflation', 'rate', 'percent', 'D'),
    ('WALCL', 'Fed total assets (H.4.1)', 'central_bank', 'flow', 'mn USD', 'W'),
    ('WRESBAL', 'US bank reserves', 'liquidity', 'flow', 'bn USD', 'W'),
    ('RRPONTSYD', 'Fed overnight reverse repo', 'liquidity', 'flow', 'bn USD', 'D'),
    ('NFCI', 'Chicago Fed financial conditions', 'financial_conditions', 'macro', 'index', 'W'),
    ('BAMLC0A0CM', 'US IG corporate OAS (ICE BofA)', 'credit', 'rate', 'percentage points', 'D'),
    ('BAMLH0A0HYM2', 'US high-yield OAS (ICE BofA)', 'credit', 'rate', 'percentage points', 'D'),
    ('DTWEXBGS', 'Broad US dollar index', 'fx', 'macro', 'index Jan 2006=100', 'D'),
    ('DCOILWTICO', 'WTI crude oil spot', 'commodities', 'commodity', 'USD per barrel', 'D'),
    ('PCOPPUSDM', 'Global copper price (IMF)', 'commodities', 'commodity', 'USD per tonne', 'M'),
)

ICE_NOTE = ('ICE BofA data: third-party copyright; FRED shows limited history and redistribution '
            'is restricted.')


class FredAdapter(Adapter):
    source = 'fred'
    license_id = 'fred'
    hosts = ('api.stlouisfed.org',)
    key_env = 'FRED_API_KEY'

    def catalog(self):
        out = []
        for nid, name, cat, kind, unit, freq in CATALOG:
            vint = freq in ('M', 'Q', 'A')
            notes = 'ALFRED vintages: point in time from first publication' if vint else \
                'current vintage only: point in time from first retrieval by this system'
            if nid.startswith('BAML'):
                notes += '. ' + ICE_NOTE
            out.append(SeriesMeta(series_id='fred:' + nid, source='fred', name=name, kind=kind,
                                  category=cat, unit=unit, frequency=freq, license_id='fred',
                                  geo='US', currency='USD' if kind == 'commodity' else '',
                                  native_id=nid, vintages=vint, notes=notes))
        return out

    def url(self, meta, key):
        q = {'series_id': meta.native_id, 'api_key': key, 'file_type': 'json',
             'observation_start': OBS_START}
        if meta.vintages:
            q.update(realtime_start='1776-07-04', realtime_end='9999-12-31')
        return API + '?' + urlencode(q)

    def fetch(self, series_ids=None, now=None):
        key = self.key()
        out = []
        for meta in self.ids(series_ids):
            body = self.get(self.url(meta, key))
            out.extend(parse(body, meta, iso(now or utc_now())))
        return out


def parse(payload, meta, retrieved_at):
    """FRED JSON -> observations. Pure."""
    try:
        doc = json.loads(payload)
    except (TypeError, ValueError):
        raise AdapterError('fred %s: response is not JSON' % meta.native_id) from None
    if 'error_message' in doc:
        raise AdapterError('fred %s: %s' % (meta.native_id, str(doc['error_message'])[:200]))
    rows = doc.get('observations')
    if not isinstance(rows, list):
        raise AdapterError('fred %s: no observations array' % meta.native_id)
    out = []
    for r in rows:
        try:
            period = period_end_from_start(r['date'], meta.frequency)
        except (KeyError, ValueError):
            continue
        value = clean_value(r.get('value'))
        if meta.vintages:
            rs = str(r.get('realtime_start') or '')[:10]
            if not rs:
                continue
            # Known by the end of the release day, or by the moment we fetched it if that is
            # earlier (a release fetched on its own day). Canonical ISO strings sort as times.
            known = min(end_of_day(rs), retrieved_at)
            vintage, published = 'alfred:' + rs, rs + 'T00:00:00Z'
        else:
            known, vintage, published = retrieved_at, 'fred:' + retrieved_at, None
        out.append(Observation(series_id=meta.series_id, period=period, value=value,
                               known_at=known, retrieved_at=retrieved_at, vintage=vintage,
                               published_at=published,
                               quality='missing' if value is None else 'ok'))
    return out
