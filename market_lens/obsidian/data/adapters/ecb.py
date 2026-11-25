"""ECB Data Portal adapter (SDMX REST, CSV). No key needed.

Covers euro-area central-bank policy, balance sheet, yields, inflation and the euro exchange
rate. The portal serves the current vintage only, so ``known_at = retrieved_at``: history is
point in time from the first retrieval by this system.
"""
import csv
import io

from .base import Adapter, AdapterError, SeriesMeta, Observation, iso, utc_now, sdmx_period_end
from ..contract import clean_value

API = 'https://data-api.ecb.europa.eu/service/data/'
START = '2000-01-01'

# (flow, key), name, category, kind, unit, frequency, currency
CATALOG = (
    (('FM', 'D.U2.EUR.4F.KR.DFR.LEV'), 'ECB deposit facility rate', 'central_bank', 'rate', 'percent', 'D', ''),
    (('FM', 'D.U2.EUR.4F.KR.MRR_FR.LEV'), 'ECB main refinancing rate', 'central_bank', 'rate', 'percent', 'D', ''),
    (('ILM', 'W.U2.C.T000000.Z5.Z01'), 'Eurosystem total assets', 'central_bank', 'flow', 'mn EUR', 'W', 'EUR'),
    (('YC', 'B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y'), 'Euro area 10y AAA govt yield', 'rates', 'rate', 'percent', 'D', ''),
    (('YC', 'B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y'), 'Euro area 2y AAA govt yield', 'rates', 'rate', 'percent', 'D', ''),
    (('ICP', 'M.U2.N.000000.4.ANR'), 'Euro area HICP inflation', 'inflation', 'macro', '% year on year', 'M', ''),
    (('ICP', 'M.U2.N.XEF000.4.ANR'), 'Euro area core HICP (ex energy, food)', 'inflation', 'macro', '% year on year', 'M', ''),
    (('EXR', 'D.USD.EUR.SP00.A'), 'EUR/USD reference rate', 'fx', 'fx', 'USD per EUR', 'D', 'USD'),
)

QUALITY_FROM_STATUS = {'A': 'ok', 'P': 'provisional', 'E': 'estimated', 'F': 'ok', 'M': 'missing',
                       'L': 'missing', 'B': 'suspect'}


def _sid(flow, key):
    return 'ecb:%s.%s' % (flow, key)


class EcbAdapter(Adapter):
    source = 'ecb'
    license_id = 'ecb'
    hosts = ('data-api.ecb.europa.eu',)

    def catalog(self):
        return [SeriesMeta(series_id=_sid(f, k), source='ecb', name=name, kind=kind, category=cat,
                           unit=unit, frequency=freq, license_id='ecb', geo='EA', currency=cur,
                           native_id='%s/%s' % (f, k), vintages=False,
                           notes='current vintage only: point in time from first retrieval')
                for (f, k), name, cat, kind, unit, freq, cur in CATALOG]

    def fetch(self, series_ids=None, now=None):
        out = []
        for meta in self.ids(series_ids):
            flow, key = meta.native_id.split('/', 1)
            url = '%s%s/%s?format=csvdata&detail=dataonly&startPeriod=%s' % (API, flow, key, START)
            out.extend(parse(self.get(url), meta, iso(now or utc_now())))
        return out


def parse(payload, meta, retrieved_at):
    """ECB SDMX CSV -> observations. Pure."""
    try:
        text = payload.decode('utf-8-sig') if isinstance(payload, bytes) else str(payload)
    except UnicodeDecodeError:
        raise AdapterError('ecb %s: response is not UTF-8' % meta.native_id) from None
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or 'TIME_PERIOD' not in reader.fieldnames or 'OBS_VALUE' not in reader.fieldnames:
        raise AdapterError('ecb %s: unexpected CSV header' % meta.native_id)
    out = []
    for r in reader:
        period = sdmx_period_end(r.get('TIME_PERIOD'))
        if not period:
            continue
        value = clean_value(r.get('OBS_VALUE'))
        status = (r.get('OBS_STATUS') or 'A').strip().upper()[:1]
        quality = 'missing' if value is None else QUALITY_FROM_STATUS.get(status, 'suspect')
        out.append(Observation(series_id=meta.series_id, period=period, value=value,
                               known_at=retrieved_at, retrieved_at=retrieved_at,
                               vintage='ecb:' + retrieved_at, quality=quality,
                               note='' if status in QUALITY_FROM_STATUS else 'OBS_STATUS=' + status))
    return out
