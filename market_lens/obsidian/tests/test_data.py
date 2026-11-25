"""Phase 2 tests: observation contract, adapters (offline payloads), validation, point-in-time
store and the Market Lens bridge. No test touches the network."""
import contextlib
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from obsidian.data import store, service, validate                      # noqa: E402
from obsidian.data.contract import Observation, SeriesMeta, iso, clean_value, parse_time  # noqa: E402
from obsidian.data.licenses import LICENSES, license_for                  # noqa: E402
from obsidian.data.adapters import fred, ecb, yahoo, get_adapter, AdapterError, ADAPTERS  # noqa: E402
from obsidian.data.adapters import marketlens as bridge                   # noqa: E402
from obsidian.data.adapters.base import period_end_from_start, sdmx_period_end  # noqa: E402

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 11, 25, 12, 0, 0, tzinfo=UTC)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.c = store.open_store(self.home, init=True)

    def tearDown(self):
        self.c.close()
        self.dir.cleanup()

    def register(self, metas):
        for m in metas:
            store.upsert_series(self.c, m)


# ----------------------------------------------------------------------------- contract
class Contract(unittest.TestCase):
    def test_period_conventions(self):
        self.assertEqual(period_end_from_start('2024-01-01', 'M'), '2024-01-31')
        self.assertEqual(period_end_from_start('2024-04-01', 'Q'), '2024-06-30')
        self.assertEqual(period_end_from_start('2023-01-01', 'A'), '2023-12-31')
        self.assertEqual(sdmx_period_end('2024-Q1'), '2024-03-31')
        self.assertEqual(sdmx_period_end('2024-02'), '2024-02-29')
        self.assertEqual(sdmx_period_end('2024-W01'), '2024-01-07')
        self.assertEqual(sdmx_period_end('2024-03-12'), '2024-03-12')
        self.assertIsNone(sdmx_period_end('garbage'))

    def test_missing_markers_are_none_not_zero(self):
        for x in ('.', '', 'NA', None, 'nan', float('inf')):
            self.assertIsNone(clean_value(x))
        self.assertEqual(clean_value('1.5'), 1.5)

    def test_timestamps_must_be_aware(self):
        with self.assertRaises(ValueError):
            iso(dt.datetime(2024, 1, 1))
        self.assertEqual(iso('2024-01-01'), '2024-01-01T00:00:00Z')

    def test_every_series_has_a_registered_license(self):
        metas = [m for n in ADAPTERS for m in get_adapter(n).catalog()] + bridge.catalog()
        self.assertGreater(len(metas), 200)
        for m in metas:
            license_for(m.license_id)
            self.assertEqual(validate.validate_meta(m), [], m.series_id)
        self.assertFalse(LICENSES['yahoo']['commercial'])


# ----------------------------------------------------------------------------- FRED / ALFRED
ALFRED_CPI = json.dumps({'observations': [
    {'realtime_start': '2024-02-13', 'realtime_end': '2024-03-11', 'date': '2024-01-01', 'value': '308.417'},
    {'realtime_start': '2024-03-12', 'realtime_end': '9999-12-31', 'date': '2024-01-01', 'value': '308.500'},
    {'realtime_start': '2024-03-12', 'realtime_end': '9999-12-31', 'date': '2024-02-01', 'value': '310.326'},
    {'realtime_start': '2024-04-10', 'realtime_end': '9999-12-31', 'date': '2024-03-01', 'value': '.'},
]}).encode()


class Fred(StoreCase):
    def meta(self, nid):
        return [m for m in fred.FredAdapter().catalog() if m.native_id == nid][0]

    def test_alfred_vintages_are_point_in_time(self):
        meta = self.meta('CPIAUCSL')
        self.register([meta])
        rows = fred.parse(ALFRED_CPI, meta, iso(NOW))
        res = store.ingest(self.c, rows, now=NOW)
        self.assertEqual(res['inserted'], 4)
        # Before the first release: nothing. Not a zero, not a guess.
        self.assertEqual(store.as_of(self.c, meta.series_id, '2024-02-12T00:00:00Z'), [])
        first = store.as_of(self.c, meta.series_id, '2024-02-20T00:00:00Z')
        self.assertEqual([(r['period'], r['value']) for r in first], [('2024-01-31', 308.417)])
        later = store.as_of(self.c, meta.series_id, '2024-03-13T00:00:00Z')
        self.assertEqual(later[0]['value'], 308.5)
        self.assertEqual(later[0]['quality'], 'revised')
        self.assertEqual(later[0]['prev_value'], 308.417)
        # The release day itself is conservative: known at the end of that day.
        self.assertEqual(len(store.as_of(self.c, meta.series_id, '2024-03-12T20:00:00Z')), 1)
        now_rows = store.as_of(self.c, meta.series_id, NOW)
        self.assertEqual(now_rows[-1]['quality'], 'missing')
        self.assertIsNone(now_rows[-1]['value'])
        self.assertEqual(len(store.revisions(self.c, meta.series_id, '2024-01-31')), 2)

    def test_release_fetched_on_its_own_day_is_not_in_the_future(self):
        meta = self.meta('CPIAUCSL')
        self.register([meta])
        payload = json.dumps({'observations': [
            {'realtime_start': '2026-11-25', 'realtime_end': '9999-12-31', 'date': '2026-10-01', 'value': '330'}]})
        rows = fred.parse(payload.encode(), meta, iso(NOW))
        self.assertEqual(rows[0].known_at, iso(NOW))
        self.assertEqual(store.ingest(self.c, rows, now=NOW)['inserted'], 1)

    def test_daily_series_are_current_vintage(self):
        meta = self.meta('DGS10')
        self.assertFalse(meta.vintages)
        url = fred.FredAdapter().url(meta, 'KEY')
        self.assertNotIn('realtime_start', url)
        self.assertIn('realtime_start', fred.FredAdapter().url(self.meta('UNRATE'), 'KEY'))
        rows = fred.parse(b'{"observations":[{"date":"2024-03-01","value":"4.2"}]}', meta, iso(NOW))
        self.assertEqual(rows[0].known_at, iso(NOW))

    def test_missing_key_is_explained_and_key_never_leaks(self):
        with patch.dict(os.environ, {'FRED_API_KEY': ''}):
            with self.assertRaisesRegex(AdapterError, 'FRED_API_KEY'):
                fred.FredAdapter().fetch(['fred:UNRATE'])
        with patch.dict(os.environ, {'FRED_API_KEY': 'SECRET123'}):
            with patch('net.fetch', side_effect=RuntimeError('boom https://api.stlouisfed.org/x?api_key=SECRET123')):
                with self.assertRaises(AdapterError) as ctx:
                    fred.FredAdapter().fetch(['fred:UNRATE'])
        self.assertNotIn('SECRET123', str(ctx.exception))

    def test_error_payload_is_an_error(self):
        with self.assertRaisesRegex(AdapterError, 'Bad Request'):
            fred.parse(b'{"error_code":400,"error_message":"Bad Request. Invalid key"}', self.meta('UNRATE'), iso(NOW))


# ----------------------------------------------------------------------------- ECB
ECB_CSV = (
    'KEY,FREQ,REF_AREA,CURRENCY,PROVIDER_FM,INSTRUMENT_FM,PROVIDER_FM_ID,DATA_TYPE_FM,TIME_PERIOD,OBS_VALUE,OBS_STATUS\n'
    'FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,4F,KR,DFR,LEV,2024-06-11,4.0,A\n'
    'FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,4F,KR,DFR,LEV,2024-06-12,3.75,A\n'
    'FM.D.U2.EUR.4F.KR.DFR.LEV,D,U2,EUR,4F,KR,DFR,LEV,2024-06-13,,M\n'
).encode()


class Ecb(StoreCase):
    def test_parse_and_store(self):
        meta = [m for m in ecb.EcbAdapter().catalog() if 'DFR' in m.series_id][0]
        self.register([meta])
        rows = ecb.parse(ECB_CSV, meta, iso(NOW))
        self.assertEqual([r.value for r in rows], [4.0, 3.75, None])
        self.assertEqual(rows[-1].quality, 'missing')
        self.assertTrue(all(r.known_at == iso(NOW) for r in rows))
        self.assertEqual(store.ingest(self.c, rows, now=NOW)['inserted'], 3)
        # Retrieved today: invisible to any earlier point-in-time query.
        self.assertEqual(store.as_of(self.c, meta.series_id, '2024-06-14T00:00:00Z'), [])

    def test_bad_header_rejected(self):
        meta = ecb.EcbAdapter().catalog()[0]
        with self.assertRaises(AdapterError):
            ecb.parse(b'<html>maintenance</html>', meta, iso(NOW))


# ----------------------------------------------------------------------------- Yahoo
def chart(closes, adj, stamps, live=None, currency='USD'):
    meta = {'currency': currency, 'symbol': 'SPY', 'gmtoffset': -18000}
    if live:
        meta['currentTradingPeriod'] = {'regular': {'start': live[0], 'end': live[1]}}
    return json.dumps({'chart': {'result': [{'meta': meta, 'timestamp': stamps, 'indicators': {
        'quote': [{'close': closes, 'volume': [1000] * len(closes)}], 'adjclose': [{'adjclose': adj}]}}],
        'error': None}}).encode()


class Yahoo(StoreCase):
    def test_close_is_pit_adjclose_is_not(self):
        d1 = int(dt.datetime(2026, 11, 23, 14, 30, tzinfo=UTC).timestamp())
        d2 = int(dt.datetime(2026, 11, 24, 14, 30, tzinfo=UTC).timestamp())
        rows = yahoo.parse(chart([600.0, 605.0], [590.0, 595.0], [d1, d2]), 'SPY', iso(NOW))
        close = [r for r in rows if r.series_id == 'yahoo:SPY:close']
        adj = [r for r in rows if r.series_id == 'yahoo:SPY:adjclose']
        self.assertEqual([r.period for r in close], ['2026-11-23', '2026-11-24'])
        # Close: known after its session (end of the local day), well before retrieval.
        self.assertEqual(close[0].known_at, '2026-11-24T04:59:59Z')
        # Adjusted: rewritten at distributions, so only known from retrieval.
        self.assertTrue(all(r.known_at == iso(NOW) for r in adj))
        self.register(yahoo.series_for('SPY'))
        self.assertEqual(store.ingest(self.c, rows, now=NOW)['rejected'], 0)
        pit = store.as_of(self.c, 'yahoo:SPY:close', '2026-11-24T06:00:00Z')
        self.assertEqual([r['value'] for r in pit], [600.0])
        self.assertEqual(store.as_of(self.c, 'yahoo:SPY:adjclose', '2026-11-24T06:00:00Z'), [])

    def test_open_session_bar_is_provisional_then_finalised(self):
        start = int(dt.datetime(2026, 11, 25, 14, 30, tzinfo=UTC).timestamp())
        end = int(dt.datetime(2026, 11, 25, 21, 0, tzinfo=UTC).timestamp())
        rows = yahoo.parse(chart([601.0], [601.0], [start], live=(start, end)), 'SPY', iso(NOW))
        close = [r for r in rows if r.series_id == 'yahoo:SPY:close'][0]
        self.assertEqual(close.quality, 'provisional')
        self.assertIn('session still open', close.note)
        self.register(yahoo.series_for('SPY'))
        store.ingest(self.c, rows, now=NOW)
        later = NOW + dt.timedelta(hours=10)
        final = yahoo.parse(chart([603.0], [603.0], [start], live=(start + 86400, end + 86400)), 'SPY', iso(later))
        store.ingest(self.c, final, now=later)
        last = store.latest(self.c, 'yahoo:SPY:close', later)
        self.assertEqual((last['value'], last['quality']), (603.0, 'ok'))  # finalised, not "revised"

    def test_non_usd_refused(self):
        with self.assertRaisesRegex(AdapterError, 'currency'):
            yahoo.parse(chart([1.0], [1.0], [1700000000], currency='EUR'), 'SAP', iso(NOW))


# ----------------------------------------------------------------------------- validation + store
class Validation(StoreCase):
    def meta(self):
        return SeriesMeta(series_id='test:X', source='test', name='x', kind='macro', category='growth',
                          unit='u', frequency='M', license_id='ecb')

    def obs(self, **kw):
        base = dict(series_id='test:X', period='2024-01-31', value=1.0, known_at=iso(NOW),
                    retrieved_at=iso(NOW), vintage='v1')
        base.update(kw)
        return Observation(**base)

    def test_rejections_are_recorded_not_stored(self):
        self.register([self.meta()])
        future = iso(NOW + dt.timedelta(days=1))
        bad = [self.obs(known_at=future, retrieved_at=future),             # known in the future
               self.obs(known_at='2024-02-01T00:00:00Z'),                  # live source known before fetch
               self.obs(period='2024-01'),                                 # not an ISO date
               self.obs(value=float('nan')),
               self.obs(series_id='test:UNKNOWN')]
        res = store.ingest(self.c, bad, now=NOW)
        self.assertEqual(res['rejected'], 5)
        self.assertEqual(self.c.execute('select count(*) from obs').fetchone()[0], 0)
        self.assertEqual(self.c.execute("select count(*) from issue where severity='error'").fetchone()[0], 5)

    def test_change_only_and_append_only(self):
        self.register([self.meta()])
        self.assertEqual(store.ingest(self.c, [self.obs()], now=NOW)['inserted'], 1)
        self.assertEqual(store.ingest(self.c, [self.obs(vintage='v2')], now=NOW)['unchanged'], 1)
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute('update obs set value=2')
        with self.assertRaises(sqlite3.DatabaseError):
            self.c.execute('delete from obs')

    def test_outlier_is_flagged_not_altered(self):
        self.register([self.meta()])
        import math
        hist = [self.obs(period='%04d-%02d-28' % (2020 + i // 12, i % 12 + 1),
                         value=100 + math.sin(i * 1.7) * 0.5, vintage='h%d' % i) for i in range(30)]
        self.assertEqual(store.ingest(self.c, hist, now=NOW)['suspect'], 0)
        res = store.ingest(self.c, [self.obs(period='2026-10-31', value=1e6, vintage='z')], now=NOW)
        self.assertEqual(res['suspect'], 1)
        last = store.latest(self.c, 'test:X', NOW)
        self.assertEqual((last['value'], last['quality']), (1e6, 'suspect'))

    def test_policy_rate_step_is_not_an_outlier(self):
        flat = [4.0] * 40
        self.assertEqual(validate.jump_outliers(flat, [3.75], 'rate'), [''])
        self.assertIn('factor', validate.jump_outliers([100.0] * 5, [400.0], 'price')[0])
        self.assertEqual(validate.jump_outliers([100.0] * 5, [101.0], 'price'), [''])

    def test_staleness(self):
        self.assertEqual(validate.staleness('2026-01-31', 'M', NOW), 'stale')
        self.assertEqual(validate.staleness('2026-10-31', 'M', NOW), 'fresh')

    def test_store_refuses_foreign_files(self):
        other = self.home / 'other.sqlite3'
        sqlite3.connect(other).execute('create table x(a)').connection.close()
        with self.assertRaises(ValueError):
            store.open_store(other)

    def test_adapter_hosts_are_enforced(self):
        with self.assertRaisesRegex(AdapterError, 'not registered'):
            ecb.EcbAdapter().get('https://evil.example.com/data')


class WebAndReadOnly(StoreCase):
    def test_read_only_connection_cannot_write(self):
        self.c.commit()
        ro = store.open_store(self.home, read_only=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("insert into cursor(name,value) values('x','y')")
        finally:
            ro.close()

    def test_web_view_routes(self):
        service.init(self.home)
        arg = lambda k: {'source': 'ecb', 'id': 'ecb:FM.D.U2.EUR.4F.KR.DFR.LEV'}.get(k, '')
        self.assertEqual(len(service.web_view(self.home, '/api/data/series', arg)), 8)
        out = service.web_view(self.home, '/api/data/asof', arg)
        self.assertEqual(out['observations'], [])          # registered, nothing fetched yet
        self.assertIn('licenses', service.web_view(self.home, '/api/data/sources', arg))
        with self.assertRaises(KeyError):
            service.web_view(self.home, '/api/data/other', arg)
        with self.assertRaises(FileNotFoundError):
            service.quality(self.home / 'missing')


# ----------------------------------------------------------------------------- Market Lens bridge
class Bridge(unittest.TestCase):
    def test_journal_revisions_become_pit_vintages(self):
        import sources as s
        import store as h
        with zipfile.ZipFile(ROOT / 'qa/offline-fixtures.zip') as f:
            meta = json.loads(f.read('test_manifest.json'))
            fix = f.read('uk-cpi')
        base = h.when(meta['clock'])
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            j = h.db(p, True)

            def put(sec, body):
                t = base + dt.timedelta(seconds=sec)
                tick = iter(range(60))
                return h.capture(j, 'uk-cpi', s.F['uk-cpi']['minutes'],
                                 lambda: t + dt.timedelta(milliseconds=next(tick)),
                                 lambda url, post: (body, url, '', ''), False)

            self.assertEqual(put(0, fix)['state'], 'accepted')
            doc = json.loads(fix)
            last = doc['months'][-1]
            old_value = float(last['value'])
            last['value'] = doc['description']['number'] = '%.1f' % (old_value + 0.7)
            self.assertEqual(put(3700, json.dumps(doc).encode())['state'], 'accepted')

            later = base + dt.timedelta(days=30)
            res = service.sync_journal(p, j, now=later)
            self.assertEqual(res['snapshots'], 2)
            self.assertGreater(res['inserted'], 10)
            self.assertEqual(res['revised'], 1)
            # Incremental: a second sync has nothing new.
            self.assertEqual(service.sync_journal(p, j, now=later)['snapshots'], 0)
            j.close()

            with contextlib.closing(store.open_store(p)) as c:
                sid = 'marketlens:ONS.D7G7'
                before = store.latest(c, sid, base + dt.timedelta(seconds=1800))
                after = store.latest(c, sid, base + dt.timedelta(seconds=7200))
                self.assertEqual(before['value'], old_value)
                self.assertAlmostEqual(after['value'], old_value + 0.7, places=6)
                self.assertEqual(after['quality'], 'revised')
                self.assertIsNone(store.latest(c, sid, base - dt.timedelta(seconds=1)))
                report = store.quality_report(c, later)
                self.assertTrue(any(r['series_id'] == sid and r['last_period'] for r in report))


if __name__ == '__main__':
    unittest.main()
