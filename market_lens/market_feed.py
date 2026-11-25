"""Daily market reference FX rates from ECB (90-day history XML)."""
import xml.etree.ElementTree as ET
from decimal import Decimal as D

CURRENCIES = {'USD': 'US', 'GBP': 'GB', 'JPY': 'JP', 'CAD': 'CA', 'CNY': 'CN', 'AUD': 'AU', 'CHF': 'CH', 'SEK': 'SE', 'NOK': 'NO', 'MXN': 'MX', 'BRL': 'BR', 'KRW': 'KR'}
K = 'ecb-daily-fx'
URL = 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml'

def register(feed, metric):
    feed(K, 'ECB', 'Daily ECB reference exchange rates (90-day rolling)', 30, 7, 'Markets')
    for cur, geo in CURRENCIES.items():
        metric(K, f'ECB.DAILY.{cur}', f'EUR/{cur} daily reference rate', f'{cur} per EUR', 'D', geo, 'FX-01',
               'Daily ECB reference rate; not an executable quote')

def request(d):
    return URL, None

def fill(o, text, day):
    from sources import guard
    root = ET.fromstring(text)
    ns = {'c': 'http://www.ecb.int/vocabulary/2002-08-01/eurofxref'}
    cubes = root.findall('.//c:Cube[@time]', ns)
    guard(len(cubes) >= 5, 'incomplete ECB daily FX XML')
    for cube in cubes:
        t = cube.attrib['time']
        for item in cube.findall('c:Cube[@currency]', ns):
            cur = item.attrib['currency']
            if cur in CURRENCIES:
                val = D(item.attrib['rate'])
                guard(val > 0 and val.is_finite(), 'invalid ECB daily FX rate')
                o.put(f'ECB.DAILY.{cur}', t, val, 'A', at=f'time={t},currency={cur}')
    return o.finish(0)

UST_K = 'us-treasury-daily'
UST_URL = 'https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv'

def register_ust(feed, metric):
    feed(UST_K, 'US Treasury', 'Daily US Treasury par yield curve rates', 60, 7, 'Markets')
    metric(UST_K, 'UST.DAILY.2Y', 'US 2-Year Treasury daily par yield', '% per annum', 'D', 'US', 'YLD-02', 'Daily US Treasury par yield')
    metric(UST_K, 'UST.DAILY.10Y', 'US 10-Year Treasury daily par yield', '% per annum', 'D', 'US', 'YLD-01', 'Daily US Treasury par yield')
    metric(UST_K, 'UST.DAILY.10Y2Y', 'US 10Y minus 2Y daily yield spread', 'percentage points', 'D', 'US', 'YLD-03', 'Daily 10Y-2Y Treasury spread')

def request_ust(d):
    return UST_URL.format(year=d.year), None

def fill_ust(o, text, day):
    import csv, io, datetime as dt
    from sources import guard
    rows = list(csv.DictReader(io.StringIO(text)))
    guard(len(rows) >= 1, 'empty US Treasury daily CSV')
    for n, r in enumerate(rows, 2):
        raw_d = r.get('Date', '').strip()
        if not raw_d:
            continue
        t = dt.datetime.strptime(raw_d, '%m/%d/%Y').date().isoformat()
        y2 = r.get('2 Yr', '').strip()
        y10 = r.get('10 Yr', '').strip()
        if y2:
            v2 = D(y2); guard(v2.is_finite(), 'invalid 2Y yield')
            o.put('UST.DAILY.2Y', t, v2, 'A', at=f'row {n} 2 Yr')
        if y10:
            v10 = D(y10); guard(v10.is_finite(), 'invalid 10Y yield')
            o.put('UST.DAILY.10Y', t, v10, 'A', at=f'row {n} 10 Yr')
        if y2 and y10:
            o.put('UST.DAILY.10Y2Y', t, D(y10) - D(y2), 'A', at=f'row {n} 10Y-2Y')
    return o.finish(0)
