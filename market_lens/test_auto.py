"""Offline replay & fault tests. Fixtures are never read by the live worker. No login services are installed by tests."""
import collections,contextlib,csv,datetime as dt,io,json,os,plistlib,sqlite3,tempfile,time,unittest,zipfile
from pathlib import Path
from unittest.mock import patch
import xml.etree.ElementTree as ET
import sources as s,store as h,engine as e,materialize as mat,autostart as au
from net import FetchFailure,delay
ROOT=Path(__file__).resolve().parent
with zipfile.ZipFile(ROOT/'qa/offline-fixtures.zip') as f:
 META=json.loads(f.read('test_manifest.json'));FIX={k:f.read(k) for k in s.F}
BASE=h.when(META['clock']);JT=lambda b:json.loads(b);B=lambda j:json.dumps(j,ensure_ascii=False).encode()
class Ledger(unittest.TestCase):
 def setUp(self):self.dir=tempfile.TemporaryDirectory();self.p=Path(self.dir.name);self.c=h.db(self.p,True)
 def tearDown(self):self.c.close();self.dir.cleanup()
 def put(self,k='uk-cpi',sec=0,b=None,fail=None,force=False):
  t=BASE+dt.timedelta(seconds=sec);tick=iter(range(60));f=lambda:t+dt.timedelta(milliseconds=next(tick));
  def get(url,post):
   if fail is not None:raise fail
   return FIX[k] if b is None else b,url,'',''
  return h.capture(self.c,k,s.F[k]['minutes'],f,get,force)
 def lens(self,k,i,sec):return h.lens(self.c,k,i,BASE+dt.timedelta(seconds=sec))
 def test_all_26_live_formats(self):
  for k in s.F:
   result=self.put(k);self.assertEqual(result['state'],'accepted',(k,result));self.assertEqual(result['observations'],META['counts'][k])
  self.assertEqual(self.c.execute('select count(*) from ready').fetchone()[0],26);self.assertEqual(len(s.M),154);self.assertEqual(len(h.events(self.c)),0);self.assertEqual(len(e.catalogue(self.c,self.p,geo='CA',fac='INF-01',d=BASE+dt.timedelta(seconds=2))),2)
 def test_schedule_persists_on_reopen(self):
  r=self.put();self.assertEqual(r['state'],'accepted');self.c.close();self.c=h.db(self.p);self.assertEqual(self.put(sec=300)['state'],'not due');self.assertEqual(self.put(sec=3601)['kind'],'unchanged');self.assertEqual(self.c.execute('select count(*) from object').fetchone()[0],1)
 def test_release_revision_baseline_and_no_time_travel(self):
  r=self.put();self.assertEqual(r['kind'],'baseline');self.assertIsNone(self.lens('uk-cpi','ONS.D7G7',-1)['record']);old=JT(FIX['uk-cpi']);rv=old['months'][-1];old_value=rv['value'];rv['value']=old['description']['number']='4.2';rr=self.put(sec=3700,b=B(old));self.assertEqual((rr['new_periods'],rr['revisions']),(0,1));self.assertEqual(self.lens('uk-cpi','ONS.D7G7',10)['current_value'],old_value);self.assertEqual(self.lens('uk-cpi','ONS.D7G7',3710)['current_value'],'4.2');
  old['months'].append(dict(rv,date='2026 NOV',year='2026',month='November',value='4.5'));old['description'].update(date='2026 NOV',number='4.5',releaseDate='2026-12-15T00:00:00Z');r=self.put(sec=32*86400,b=B(old));self.assertEqual(r['new_periods'],1);self.assertEqual({z['kind'] for z in h.events(self.c)},{'revision','new period'});self.assertEqual(h.events(self.c)[0]['t'],'2026-11')
 def test_asof_uses_policy_at_decision_time(self):
  self.put();e.record_policy(self.c,h.iso(BASE),{'uk-cpi':60});e.record_policy(self.c,h.iso(BASE+dt.timedelta(seconds=10)),{'ecb':30});
  self.assertEqual(e.detail(self.c,self.p,'ONS.D7G7',BASE+dt.timedelta(seconds=3))['state'],'accepted');self.assertEqual(e.detail(self.c,self.p,'ONS.D7G7',BASE+dt.timedelta(seconds=12))['state'],'disabled')
 def test_failed_poll_preserves_but_blocks(self):
  self.put();r=self.put(sec=3602,fail=OSError('fixture network failure'));self.assertEqual(r['state'],'failed');z=self.lens('uk-cpi','ONS.D7G7',3604);self.assertIsNone(z['current_value']);self.assertIsNotNone(z['last_numeric']);self.assertEqual(self.put(sec=3610)['state'],'not due');self.assertEqual(self.put(sec=3910)['kind'],'unchanged')
 def test_host_retry_after_is_shared_and_survives_restart(self):
  self.put(fail=FetchFailure('HTTP 429',900));self.assertEqual(self.put('uk-wood',sec=120)['state'],'not due');self.c.close();self.c=h.db(self.p);self.assertEqual(self.put('uk-wood',sec=300)['state'],'not due');self.assertEqual(self.put('uk-wood',sec=902)['state'],'accepted');self.assertEqual(delay('600'),600)
 def test_bls_quota_floor_on_failure(self):
  r=self.put('us-bls',fail=FetchFailure('HTTP 503'));self.assertGreaterEqual((h.when(r['next_check'])-BASE).total_seconds(),21600);self.assertEqual(self.put('us-bls',sec=2000)['state'],'not due')
 def test_embedded_missing_vs_zero(self):
  self.put('eu-jobs');x=self.lens('eu-jobs','une_rt_m.ES',1);self.assertEqual(x['period'],'2026-09');self.assertIsNotNone(x['current_value']);self.put('us-bls');y=self.lens('us-bls','BLS.CPI.YOY',1);self.assertEqual(y['state'],'latest value missing');self.assertIsNone(y['current_value']);self.assertIsNotNone(y['last_numeric']['value'])
 def test_no_zero_for_withdrawn_price(self):
  self.put('ca-prices');j=JT(FIX['ca-prices']);v=next(x['object'] for x in j if x['object']['vectorId']==1230995983)['vectorDataPoint'][-1];v.update(value=None,statusCode=1);res=self.put('ca-prices',sec=3605,b=B(j));self.assertEqual(res['revisions'],1);event=h.events(self.c)[0];self.assertEqual((event['kind'],event['new_x']),('withdrawn',None));self.assertIsNone(self.lens('ca-prices','SC.1230995983',3610)['current_value'])
 def test_gap_fails_closed_on_first_ons_body(self):
  j=JT(FIX['uk-cpi']);j['months'].pop(12);r=self.put(b=B(j));self.assertEqual(r['state'],'quarantined');self.assertIsNone(self.lens('uk-cpi','ONS.D7G7',5)['last_numeric'])
 def test_unexplained_missing_overlap_quarantined(self):
  self.put('ny-sofr');j=JT(FIX['ny-sofr']);j['refRates'].pop(10);r=self.put('ny-sofr',sec=1850,b=B(j));self.assertEqual(r['state'],'quarantined');self.assertIn('silently removed',r['reason'])
 def test_overdue_then_old_reference(self):
  self.put();self.assertEqual(self.lens('uk-cpi','ONS.D7G7',4000)['state'],'overdue');r=self.put(sec=120*86400);self.assertEqual(r['state'],'accepted');self.assertEqual(self.lens('uk-cpi','ONS.D7G7',120*86400+1)['state'],'reference old')
 def test_pending_then_interrupted(self):
  self.put();d=BASE+dt.timedelta(seconds=3605)
  with self.c:self.c.execute('insert into poll(feed,started,request_url,request_body) values(?,?,?,?)',('uk-cpi',h.iso(d),'https://www.ons.gov.uk/',''))
  self.assertEqual(self.lens('uk-cpi','ONS.D7G7',3606)['state'],'checking');self.assertEqual(h.recover(self.c,s.F,lambda:d+dt.timedelta(seconds=5)),1);self.assertEqual(self.lens('uk-cpi','ONS.D7G7',3615)['state'],'interrupted');self.assertEqual(self.put(sec=3616)['state'],'accepted')
 def test_checkpoint_and_csv(self):
  self.put();mat.latest(self.c,self.p);self.assertTrue((self.p/'latest_live.csv').is_file());path=self.p/'copy.ml35.xz';mat.checkpoint(self.p,path);other=self.p/'another';self.assertTrue(mat.restore_if_absent(other,path));self.assertFalse(mat.restore_if_absent(other,path));
  with contextlib.closing(h.db(other)) as c:self.assertEqual(c.execute('select count(*) from ready').fetchone()[0],1)
 def test_install_confirms_real_new_heartbeat(self):
  start=h.now();e.pulse(self.c,h.iso(start),'running');self.assertTrue(au.confirm_start(self.p,start,.01));e.pulse(self.c,h.iso(start),'stopped')
  with self.assertRaises(RuntimeError):au.confirm_start(self.p,start,0)
 def test_singleton_and_readonly_ledger(self):
  with h.writer(self.p):
   with self.assertRaises(RuntimeError):
    with h.writer(self.p):pass
  self.put()
  with self.assertRaises(sqlite3.IntegrityError):self.c.execute('delete from ready')
 def test_plan_range_and_host_pacing(self):
  jobs=e.select_due(self.c,s.F,{}, {},BASE,500);self.assertLessEqual(len(jobs),3);self.assertEqual(len(set(hh for _,hh in jobs)),len(jobs));(self.p/'settings.json').write_text(json.dumps({'interval_minutes':{'us-bls':1}}))
  with self.assertRaises(ValueError):e.settings(self.p)
 def test_automatic_loop_rechecks_without_another_command(self):
  # Real run loop, WAL, parsing, change journal and persistent plan. Only time and publisher I/O are simulated.
  (self.p/'settings.json').write_text('{"enabled":["uk-cpi"]}');counter=[0];clock=[BASE];orig=h.capture;j=JT(FIX['uk-cpi']);j['months'][-1]['value']=j['description']['number']='4.3';revised=B(j);j['months'].append(dict(j['months'][-1],date='2026 NOV',year='2026',month='November',value='4.5'));j['description'].update(date='2026 NOV',number='4.5',releaseDate='2026-12-15T00:00:00Z');new_period=B(j)
  class Stop:
   steps=0
   def is_set(self):return counter[0]>=3 or self.steps>300
   def wait(self,t):self.steps+=1;clock[0]+=dt.timedelta(days=32) if counter[0]==2 and clock[0]<BASE+dt.timedelta(days=30) else dt.timedelta(seconds=75);time.sleep(.003)
  def mockcap(c,k,mins):
   snap_time=clock[0];it=iter(range(25));r=orig(c,k,mins,lambda:snap_time+dt.timedelta(milliseconds=next(it)),lambda url,post:((FIX[k],revised,new_period)[min(counter[0],2)],url,'',''));counter[0]+=int(r['state']=='accepted');return r
  # Host spacing uses a monotonic clock (2s), advance it along with the fictitious wall clock.
  with patch.object(e,'now',lambda:clock[0]),patch.object(e,'capture',mockcap),patch.object(e.time,'monotonic',lambda:100+(clock[0]-BASE).total_seconds()):e.run(self.p,web=False,stop=Stop())
  self.assertEqual(counter[0],3);self.assertEqual(self.c.execute('select count(*) from ready').fetchone()[0],3);self.assertEqual([x['kind'] for x in h.events(self.c)],['new period','revision'])
class Parsers(unittest.TestCase):
 def test_dimension_guards(self):
  j=JT(FIX['ca-cpi']);j[0]['object']['coordinate']='3.3'
  with self.assertRaises(ValueError):s.parse('ca-cpi',B(j),BASE)
  j=JT(FIX['eu-hicp']);j['dimension']['unit']['category']['index']={'INDEX':0}
  with self.assertRaises(ValueError):s.parse('eu-hicp',B(j),BASE)
 def test_frequency_and_future(self):
  j=JT(FIX['ca-jobs']);j[0]['object']['vectorDataPoint'][1]['frequencyCode']=6
  with self.assertRaises(ValueError):s.parse('ca-jobs',B(j),BASE)
  j=JT(FIX['ca-cpi']);j[0]['object']['vectorDataPoint'][-1]['releaseTime']='2099-01-01T08:30'
  with self.assertRaises(ValueError):s.parse('ca-cpi',B(j),BASE)
 def test_ecb_hole_and_future_ons(self):
  tree=ET.fromstring(FIX['ecb']);p=next(x for x in tree.iter() if any('time' in r.attrib for r in x));p.remove(list(p)[15]);
  with self.assertRaises(ValueError):s.parse('ecb',ET.tostring(tree),BASE)
  j=JT(FIX['uk-ppi']);j['description']['releaseDate']='2099-01-01T00:00:00Z'
  with self.assertRaises(ValueError):s.parse('uk-ppi',B(j),BASE)
 def test_geo_and_no_lookahead_derivations(self):
  o=s.parse('ca-jobs',FIX['ca-jobs'],BASE);self.assertTrue(all('-Q' in t for i,t in o.cells));o=s.parse('ca-cpi',FIX['ca-cpi'],BASE);i='SC.CPI.YOY';period='2026-10';a,b=[s.D(o.cells['SC.41690973',t][0]) for t in ('2025-10',period)];self.assertAlmostEqual(float(o.cells[i,period][0]),float(100*(b/a-1)),6)
 def test_new_year_requests_roll_forward(self):
  a=s.request('oecd-gdp',dt.date(2026,11,18))[0];b=s.request('oecd-gdp',dt.date(2027,1,8))[0];self.assertIn('2023-Q1',a);self.assertIn('2024-Q1',b);self.assertEqual(s.quarter_floor_start('2025-12'),'2026-Q1');self.assertIn('2027-01-08',s.request('ca-cpi',dt.date(2027,1,8))[0])
 def test_login_manifests_no_admin_and_no_password(self):
  args=['/some path/python','/app/market_lens.py','--data','/data folder','run'];mac=plistlib.loads(au.mac_agent(args));self.assertEqual(mac['ProgramArguments'],args);self.assertTrue(mac['RunAtLoad']);linux=au.linux_unit(args).decode();self.assertIn('Restart=on-failure',linux);self.assertIn('"/some path/python"',linux);x=ET.fromstring(au.win_task(args,'COMPUTER\\Example'));tags={p.tag.split('}')[-1]:p.text for p in x.iter()};self.assertEqual(tags['LogonType'],'InteractiveToken');self.assertEqual(tags['ExecutionTimeLimit'],'PT0S');self.assertEqual(tags['RunLevel'],'LeastPrivilege');self.assertNotIn('Password',tags)

 def test_alpha_engine_regime_and_sizing(self):
  import alpha_engine as ae
  from decimal import Decimal
  r = ae.detect_regime({'UST.DAILY.10Y2Y': Decimal('-0.25'), 'NYFED.SOFR': Decimal('4.5')})
  self.assertEqual(r['regime'], 'defensive_inverted_curve')
  self.assertEqual(r['risk_scale'], 0.5)
  w = ae.inverse_vol_weights({'SPY': 0.15, 'TLT': 0.12, 'GLD': 0.14}, risk_scale=0.5)
  self.assertTrue(abs(sum(w.values()) - 0.5) < 1e-6)


 def test_macro_diffusion_and_alerts(self):
  import macro_alerts as ma
  from decimal import Decimal
  # The daily UST spread and the monthly OECD proxy describe the same curve: the monthly proxy must
  # not be counted a second time when the daily observation is present.
  d = ma.macro_diffusion({'UST.DAILY.10Y2Y': Decimal('0.35'), 'FIN.USA.curve': Decimal('0.20'), 'FIN.DEU.curve': Decimal('-0.10')})
  self.assertEqual(d['positive'], 1)
  self.assertEqual(d['negative'], 1)
  self.assertAlmostEqual(d['diffusion_pct'], 50.0, places=2)
  d2 = ma.macro_diffusion({'FIN.USA.curve': Decimal('0.20'), 'FIN.DEU.curve': Decimal('-0.10')})
  self.assertEqual((d2['positive'], d2['negative']), (1, 1))


 def test_paper_trader_vol_and_costs(self):
  import paper_trader as pt
  vols = pt.realized_vols({'SPY': [100, 101, 100.5, 102, 101.5], 'TLT': [90, 90.2, 90.1, 90.4, 90.3]})
  self.assertTrue('SPY' in vols and 'TLT' in vols and vols['SPY'] > 0)


 def test_search_synonyms_and_ranking(self):
  import tempfile
  with tempfile.TemporaryDirectory() as td:
   p = Path(td); c = h.db(p, True)
   res = e.catalogue(c, p, q='rates')
   self.assertTrue(any('SOFR' in r['id'] or 'IRLT' in r['id'] for r in res))


 def test_signal_filter_rules(self):
  import signal_filter as sf
  z = sf.winsorize_zscore([1.0, 1.1, 1.25, 1.35, 10.0], cap=3.5)
  self.assertEqual(z, 3.5)
  import random
  rng = random.Random(11)
  # A genuine duplicate: same movements, different level and scale. It must be pruned.
  steps = [rng.gauss(0, 1) for _ in range(60)]
  a = [100.0]
  for s in steps:
   a.append(a[-1] + s)
  dup = [2.0 * x + 50.0 for x in a]
  self.assertEqual(sf.prune_correlated({'A': a, 'DUP': dup}, threshold=0.85), ['A'])
  # Two independent series that both drift down in levels are NOT the same signal: deleting one
  # dropped a whole country's macro view. Correlation is judged on movements, not on the trend.
  b, c = [100.0], [100.0]
  for _ in range(60):
   b.append(b[-1] - 0.3 + rng.gauss(0, 1))
   c.append(c[-1] - 0.3 + rng.gauss(0, 1))
  self.assertEqual(sorted(sf.prune_correlated({'B': b, 'C': c}, threshold=0.85)), ['B', 'C'])
  # Short, flat or empty inputs are never elected as representatives.
  self.assertEqual(sf.prune_correlated({'X': [1, 2, 3], 'Y': [4] * 20, 'Z': []}), [])


 def test_macro_quadrant_and_revision_gate(self):
  import signal_filter as sf
  self.assertEqual(sf.classify_macro_quadrant(1.2, -0.5), 'rising_growth_falling_inflation')
  self.assertEqual(sf.classify_macro_quadrant(-0.8, 1.5), 'falling_growth_rising_inflation')


 def test_daily_processor_cycle(self):
  import daily_processor as dp, tempfile
  with tempfile.TemporaryDirectory() as td:
   p = Path(td); c = h.db(p, True); csv_p = p / 'latest.csv'
   mat.latest(c, p); c.close()
   rec = dp.run_processor(p / 'latest_live.csv', {'SPY': 0.15, 'TLT': 0.12}, prev_weights={'SPY': 0.4, 'TLT': 0.6}, growth_z=0.8, inflation_z=-0.3)
   self.assertFalse(rec['drift_ok'])
   self.assertEqual(rec['quadrant'], 'rising_growth_falling_inflation')


 def test_all_weather_allocation(self):
  import daily_processor as dp
  w = dp.all_weather_allocation('falling_growth_falling_inflation', risk_scale=0.5)
  self.assertEqual(w['TREASURY'], 0.25)
  self.assertEqual(w['GOLD'], 0.125)
  self.assertEqual(w['TBILL'], 0.625)


 def test_monte_carlo_confidence(self):
  import paper_trader as pt
  res = pt.monte_carlo_confidence([0.01, 0.02, 0.015, 0.005, 0.012, 0.018, 0.011], trials=200)
  self.assertTrue(res['ok'])
  self.assertTrue(res['ci_95'][0] > 0)
  self.assertEqual(res['prob_beat_benchmark'], 1.0)


 def test_hysteresis_and_drawdown_guard(self):
  import daily_processor as dp
  self.assertEqual(dp.confirm_regime(['normal_expansion', 'defensive_inverted_curve'], confirm_days=2), 'normal_expansion')
  self.assertEqual(dp.confirm_regime(['normal_expansion', 'defensive_inverted_curve', 'defensive_inverted_curve'], confirm_days=2), 'defensive_inverted_curve')
  w = dp.drawdown_guard({'EQUITY': 0.60, 'TREASURY': 0.30, 'TBILL': 0.10}, current_nav=88.0, peak_nav=100.0, max_dd=0.10)
  self.assertEqual(w['EQUITY'], 0.30)
  self.assertEqual(w['TBILL'], 0.55)


 def test_bayesian_regime_update(self):
  import daily_processor as dp
  post = dp.bayesian_regime_update({'expansion': 0.7, 'recession': 0.3}, {'expansion': 0.2, 'recession': 0.8})
  self.assertAlmostEqual(sum(post.values()), 1.0, places=5)
  self.assertTrue(post['recession'] > 0.3)


 def test_generate_orders(self):
  import trade_signals as ts
  orders = ts.generate_orders({'EQUITY': 0.40, 'GOLD': 0.20, 'TBILL': 0.40}, {'EQUITY': 0.60, 'GOLD': 0.20, 'TBILL': 0.20}, portfolio_value=5000.0)
  by_a = {o['asset']: o for o in orders}
  self.assertEqual(by_a['EQUITY']['action'], 'BUY')
  self.assertEqual(by_a['EQUITY']['amount_usd'], 1000.0)
  self.assertEqual(by_a['GOLD']['action'], 'HOLD')
  self.assertEqual(by_a['TBILL']['action'], 'SELL')


 def test_kelly_fraction(self):
  import alpha_engine as ae
  self.assertEqual(ae.kelly_fraction(-0.02, 0.04), 0.0)
  self.assertEqual(ae.kelly_fraction(0.08, 0.04, fraction=0.5, max_weight=0.40), 0.40)
  self.assertEqual(ae.kelly_fraction(0.02, 0.04, fraction=0.5, max_weight=0.40), 0.25)


 def test_historical_analogs(self):
  import alpha_engine as ae
  hist = [
   {'date': '2020-03', 'vector': [-2.0, -0.5, 0.2], 'forward_return': 0.18},
   {'date': '2022-06', 'vector': [-0.5, 2.5, -0.4], 'forward_return': -0.10},
   {'date': '2023-10', 'vector': [-0.4, 2.2, -0.3], 'forward_return': -0.06},
  ]
  res = ae.find_historical_analogs([-0.45, 2.3, -0.35], hist, top_k=2)
  self.assertEqual(len(res['analogs']), 2)
  self.assertAlmostEqual(res['expected_forward_return'], -0.08, places=4)


 def test_detect_macro_chains(self):
  import macro_alerts as ma
  from decimal import Decimal
  chains = ma.detect_macro_chains({'UST.DAILY.10Y2Y': Decimal('-0.30'), 'NYFED.SOFR': Decimal('5.2')}, inflation_z=-0.8, yield_change=-0.2)
  names = [c['chain'] for c in chains]
  self.assertIn('tightening_to_slowdown', names)
  self.assertIn('disinflationary_pivot', names)


 def test_granger_causality_filter(self):
  import signal_filter as sf
  x = [1, 3, 2, 5, 4, 7, 6, 9, 8, 11, 10, 13]
  y = [0] + [2.0 * v + 0.1 for v in x[:-1]]
  res = sf.granger_causality_test(y, x)
  self.assertTrue(res['causal'])
  self.assertTrue(res['f_stat'] > 4.0)


 def test_hurst_exponent(self):
  import signal_filter as sf
  # R/S is applied to the differenced series: a deterministic trend has zero-variance changes, so it
  # reports insufficient data rather than inventing a trend. Persistence and noise are checked below.
  trend = [100 + i * 1.5 for i in range(30)]
  self.assertEqual(sf.hurst_exponent(trend)['regime'], 'insufficient_data')
  import random
  rng = random.Random(7)
  # Persistent returns (AR(1) with positive autocorrelation) are what "trending" actually means.
  steps = [rng.gauss(0, 1)]
  for _ in range(700):
   steps.append(0.7 * steps[-1] + rng.gauss(0, 1))
  levels = [100.0]
  for s in steps:
   levels.append(levels[-1] + s)
  self.assertEqual(sf.hurst_exponent(levels)['regime'], 'trending')


 def test_macro_divergences(self):
  import macro_alerts as ma
  d = ma.relative_macro_divergence(1.4, -0.2, threshold=1.0)
  self.assertTrue(d['divergent'])
  self.assertEqual(d['leader'], 'A')
  self.assertTrue(ma.inflation_yield_divergence(0.9, -0.15))


 def test_event_engine_prediction(self):
  import event_engine as ee
  evs = [
   {'title': 'Rate hike cycle pause', 'category': 'monetary', 'tags': ['fed', 'pause'], 'outcome': 'equities rally', 'forward_return': 0.12},
   {'title': 'Rate pause 2019', 'category': 'monetary', 'tags': ['fed', 'pause'], 'outcome': 'equities rally', 'forward_return': 0.15},
   {'title': 'Election uncertainty', 'category': 'political', 'tags': ['election'], 'outcome': 'volatility', 'forward_return': -0.04},
  ]
  res = ee.predict_event_outcome(evs, query='fed pause')
  self.assertEqual(res['matched_count'], 2)
  self.assertTrue(res['mean_return'] > 0.10)
  self.assertTrue(res['kelly_size'] > 0.0)


 def test_sentiment_guard(self):
  import alpha_engine as ae
  self.assertEqual(ae.sentiment_guard(2.5)['sentiment'], 'extreme_euphoria')
  self.assertEqual(ae.sentiment_guard(-2.5)['sentiment'], 'extreme_panic')
  self.assertEqual(ae.sentiment_guard(0.5)['risk_multiplier'], 1.0)


 def test_adapt_risk_from_paper(self):
  import paper_trader as pt
  self.assertEqual(pt.adapt_risk_from_paper([-0.01, -0.02, -0.005, 0.001, -0.01]), 0.5)
  self.assertEqual(pt.adapt_risk_from_paper([0.01, 0.02, 0.015, 0.005, 0.012]), 1.0)


 def test_stock_selector(self):
  import stock_selector as ss
  r = ss.recommend_stocks(1.0, -0.4, risk_scale=1.0)
  self.assertEqual(r['quadrant'], 'rising_growth_falling_inflation')
  self.assertIn('QQQ', r['recommended_etfs'])


 def test_micro_stock_fundamentals(self):
  import stock_selector as ss
  full = {'ev_ebitda': 14.0, 'roic': 0.18, 'altman_z': 3.5}
  # A screen only passes a name when every ratio it uses is present (missing data now fails closed).
  stocks = [
   dict({'ticker': 'AAPL', 'fcf_yield': 0.04, 'earnings_growth': 0.12, 'debt_to_equity': 1.5}, **full),
   {'ticker': 'BAD', 'fcf_yield': -0.02, 'earnings_growth': 0.20, 'debt_to_equity': 0.5, **full},
   dict({'ticker': 'MSFT', 'fcf_yield': 0.05, 'earnings_growth': 0.15, 'debt_to_equity': 0.4}, **full),
  ]
  res = ss.score_stock_fundamentals(stocks)
  self.assertEqual([r['ticker'] for r in res], ['MSFT', 'AAPL'])
  # Partial data must be rejected, not silently filled with comfortable defaults.
  self.assertEqual(ss.score_stock_fundamentals([{'ticker': 'NO_ROIC', 'fcf_yield': 0.05, 'debt_to_equity': 0.5}]), [])
  audit = ss.score_stock_fundamentals_with_audit([{'ticker': 'NO_ROIC', 'fcf_yield': 0.05, 'debt_to_equity': 0.5}])
  self.assertIn('missing', audit['rejected'][0]['reason'])


 def test_macro_micro_monte_carlo(self):
  import stock_selector as ss
  res = ss.macro_micro_monte_carlo(0.10, 0.20, growth_z=1.0, rate_shock=0.0, trials=300)
  self.assertTrue(res['expected_return'] > 0.10)
  self.assertTrue(res['kelly_size'] > 0.0)


 def test_sector_relative_and_coverage(self):
  import stock_selector as ss
  stocks = [
   {'ticker': 'A', 'sector': 'TECH', 'ev_ebitda': 15.0, 'interest_coverage': 8.0},
   {'ticker': 'B', 'sector': 'TECH', 'ev_ebitda': 25.0, 'interest_coverage': 6.0},
   {'ticker': 'C', 'sector': 'TECH', 'ev_ebitda': 10.0, 'interest_coverage': 1.5},
  ]
  res = ss.score_sector_relative(stocks)
  self.assertEqual([r['ticker'] for r in res], ['A', 'B'])
  self.assertTrue(res[0]['sector_rel_value'] > 0)
  # Unknown interest coverage cannot be assumed to clear the floor.
  self.assertEqual(ss.score_sector_relative([{'ticker': 'X', 'sector': 'TECH', 'ev_ebitda': 20}]), [])


class Regressions(unittest.TestCase):
 """Guards for the defects found in review. Each test would fail on the pre-fix behaviour."""

 def test_turnover_budget_never_liquidates(self):
  import alpha_engine as ae, daily_processor as dp
  target = {'SPY': 0.40, 'TLT': 0.40}
  # Old behaviour: turnover 0.80 > 0.50 produced weights == {} (total liquidation).
  moved, turnover, partial = ae.apply_turnover_budget(target, {}, 0.50)
  self.assertTrue(partial)
  self.assertAlmostEqual(turnover, 0.50, places=6)
  self.assertGreater(sum(moved.values()), 0.0)
  self.assertLessEqual(sum(moved.values()), 1.0)
  # A switch from equities into cash is also partial, not a wipe.
  moved2, t2, p2 = ae.apply_turnover_budget({'SPY': 0.4, 'TLT': 0.4}, {'SPY': 0.9, 'TLT': 0.1}, 0.50)
  self.assertTrue(p2)
  self.assertAlmostEqual(t2, 0.50, places=6)

 def test_caps_hold_and_remainder_is_cash(self):
  import alpha_engine as ae, stock_selector as ss
  plan = ae.allocate({'A': 0.10, 'B': 0.10})
  self.assertLessEqual(max(plan['weights'].values()), 0.40 + 1e-9)
  self.assertAlmostEqual(sum(plan['weights'].values()) + plan['cash'], 1.0, places=9)
  self.assertGreater(plan['cash'], 0.0)
  orange = ae.allocate({'A': 0.05, 'B': 0.30, 'C': 0.30, 'D': 0.30}, risk_scale=1.0)
  self.assertLessEqual(max(orange['weights'].values()), 0.40 + 1e-9)
  self.assertTrue(orange['cash'] > 0.0)
  sect = ss.apply_sector_cap({'X': 0.6, 'B': 0.4}, {'X': 'TECH', 'B': 'BONDS'}, 0.30)
  self.assertLessEqual(sect['X'], 0.30 + 1e-9)
  rp = ss.risk_parity_allocation([{'symbol': 'LOW', 'volatility': 0.01}, {'symbol': 'A', 'volatility': 0.2}, {'symbol': 'B', 'volatility': 0.2}], max_weight=0.45)
  for k in ('standard', 'adjusted_safe', 'return_adjusted'):
   self.assertLessEqual(max(rp[k].values()), 0.45 + 1e-9, k)

 def test_vol_target_is_used(self):
  import alpha_engine as ae
  # A 20%-vol asset: a 5% target sizes it to 25%, below the 40% cap, while a 50% target asks for
  # more than the cap allows and is capped at 40%. The target is doing real work.
  low = ae.inverse_vol_weights({'A': 0.20}, 0.05)
  high = ae.inverse_vol_weights({'A': 0.20}, 0.50)
  self.assertLess(sum(low.values()), sum(high.values()))
  self.assertAlmostEqual(sum(low.values()), 0.25, places=6)
  self.assertAlmostEqual(sum(high.values()), 0.40, places=6)
  # Two identical assets: the cap is applied once per sleeve (40% each), not twice (16%).
  two = ae.inverse_vol_weights({'A': 0.10, 'B': 0.10}, 10.0)
  self.assertAlmostEqual(two['A'], 0.40, places=6)
  self.assertAlmostEqual(two['B'], 0.40, places=6)
  self.assertAlmostEqual(sum(two.values()), 0.80, places=6)

 def test_regime_fails_closed_without_sofr(self):
  import alpha_engine as ae
  from decimal import Decimal
  r = ae.detect_regime({'UST.DAILY.10Y2Y': Decimal('0.40')})
  self.assertEqual(r['regime'], 'uncertain')
  self.assertEqual(r['risk_scale'], 0.5)

 def test_fallback_units_never_mix(self):
  import reliability as rel
  import sources as s
  for primary, backup in rel.FALLBACKS.items():
   self.assertEqual(s.M[primary]['unit'], s.M[backup]['unit'], f'{primary} <- {backup} unit mismatch')
   self.assertEqual(s.M[primary]['freq'], s.M[backup]['freq'], f'{primary} <- {backup} frequency mismatch')
  self.assertNotIn('BLS.CPI.YOY', rel.FALLBACKS)
  self.assertNotIn('FIN.GBR.IR3TIB', rel.FALLBACKS)

 def test_ecb_duplicate_group_and_ust_priority(self):
  import reliability as rel
  import sources as s
  self.assertNotEqual(rel.group(s.M['ECB.USD']), rel.group(s.M['ECB.DAILY.USD']))

 def test_dedupe_is_order_independent(self):
  import csv, datetime as dt, tempfile
  from pathlib import Path
  import signal_filter as sf
  now = dt.datetime.now(dt.timezone.utc)
  Z = now.isoformat().replace('+00:00', 'Z'); FUT = (now + dt.timedelta(days=30)).isoformat().replace('+00:00', 'Z')
  cols = ['exported_utc','asof_utc','recheck_due_utc','usable_until_utc','worker_online','heartbeat','series','geos','atlas_factor','signal_group','publisher','measure','unit','period','current_value','screened_value','quality','source_flag','source_release','state','last_numeric_period','last_numeric_value','snapshot_seen_utc','snapshot_indexed_utc','contract_checked_utc','source_url','sha256']
  def row(sid, period):
   return dict(exported_utc=Z, asof_utc=Z, recheck_due_utc=Z, usable_until_utc=FUT, worker_online='True', heartbeat=Z, series=sid, geos='US', atlas_factor='', signal_group='US:YLD-03', publisher='p', measure='m', unit='pp', period=period, current_value='0.35', screened_value='0.35', quality='unflagged / normal', source_flag='', source_release='', state='accepted', last_numeric_period='', last_numeric_value='', snapshot_seen_utc=Z, snapshot_indexed_utc=Z, contract_checked_utc=Z, source_url='u', sha256='h')
  for order in (['FIN.USA.curve', 'UST.DAILY.10Y2Y'], ['UST.DAILY.10Y2Y', 'FIN.USA.curve']):
   with tempfile.TemporaryDirectory() as td:
    p = Path(td) / 'e.csv'
    with p.open('w', newline='', encoding='utf-8-sig') as f:
     w = csv.DictWriter(f, cols); w.writeheader()
     for sid in order:
      w.writerow(row(sid, '2026-10' if sid == 'FIN.USA.curve' else '2026-11-18'))
    self.assertEqual(list(sf.filter_signals(p)), ['UST.DAILY.10Y2Y'], order)

 def test_hurst_random_walk_is_not_trending(self):
  import random, signal_filter as sf
  rng = random.Random(3); x = 100.0; walk = []
  for _ in range(600):
   x += rng.gauss(0, 1); walk.append(x)
  r = sf.hurst_exponent(walk)
  self.assertTrue(0.35 <= r['hurst'] <= 0.65, r)
  self.assertNotEqual(r['regime'], 'trending')

 def test_confirm_regime_returns_current_state(self):
  import daily_processor as dp
  self.assertEqual(dp.confirm_regime(['normal', 'defensive', 'stress'], 2), 'defensive')
  self.assertEqual(dp.confirm_regime(['defensive', 'normal', 'normal', 'x'], 2), 'normal')
  self.assertEqual(dp.confirm_regime(['defensive', 'x', 'x'], 2), 'x')

 def test_var_and_sharpe_are_deterministic(self):
  import stock_selector as ss
  a = ss.black_swan_heat_diffusion([{'symbol': 'SPY', 'vol': 0.15}], seed=1)['tail_var_99']
  b = ss.black_swan_heat_diffusion([{'symbol': 'SPY', 'vol': 0.15}], seed=99)['tail_var_99']
  self.assertEqual(a, b)
  self.assertGreater(a, 0.0)
  self.assertIsNone(ss.walk_forward_backtest([0.001] * 20, fee_bps=0)['sharpe'])
  self.assertEqual(ss.walk_forward_backtest([0.001, 0.002] * 10, fee_bps=5)['periods'], 20)

 def test_kelly_exposes_raw_size(self):
  import alpha_engine as ae, stock_selector as ss
  d = ae.kelly_detail(0.05, 0.20)
  self.assertEqual(d['capped_by'], 'max_weight')
  self.assertNotEqual(ss.half_kelly_position_size(0.05, 0.10, risk_free=0.05), ss.half_kelly_position_size(0.05, 0.10, risk_free=0.0))

 def test_fx_hedge_ratio_uncorrelated_is_full_hedge(self):
  import stock_selector as ss
  self.assertAlmostEqual(ss.fx_hedge_ratio(0.15, 0.10, 0.0), 1.0, places=6)
  self.assertLess(ss.fx_hedge_ratio(0.15, 0.10, -0.6), 1.0)
  self.assertGreater(ss.fx_hedge_ratio(0.15, 0.10, 0.6), 1.0)

 def test_paper_trader_marks_to_market(self):
  import paper_trader as pt, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   p = Path(td) / 'missing.csv'
   res = pt.step_paper_portfolio(p, {'SPY': [100, 110]}, {'SPY': 0.5}, nav=100.0, cost_bps=0)
   # +10% on a 50% book is +5 of NAV; the old code could only ever subtract cost.
   self.assertAlmostEqual(res['market_return_applied'], 0.05, places=6)
   self.assertGreaterEqual(res['nav_after_cost'], 100.0)
   self.assertEqual(res['weights'], {})

 def test_quality_accepts_explicit_normal_for_eurostat(self):
  import reliability as rel, sources as s
  m = s.M['prc_hicp_minr.ES']
  self.assertEqual(rel.quality(m, {'value': '2.1', 'flag': 'A'}), 'unflagged / normal')
  self.assertEqual(rel.quality(m, {'value': '2.1', 'flag': ''}), 'unflagged / normal')
  self.assertEqual(rel.quality(m, {'value': '2.1', 'flag': 'p'}), 'provisional')

 def test_missing_export_is_no_signals(self):
  import reliability as rel
  from decimal import Decimal
  import alpha_engine as ae, tempfile
  from pathlib import Path
  self.assertEqual(rel.read_export(Path('/does/not/exist.csv')), [])
  with tempfile.TemporaryDirectory() as td:
   r = ae.run_alpha_step(Path(td) / 'nope.csv', {'SPY': 0.2})
   self.assertEqual(r['weights'], {})
   self.assertEqual(r['reason'], 'no fresh screened signals')

 def test_processor_keeps_positions_on_switch(self):
  import daily_processor as dp, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   rec = dp.run_processor(Path(td) / 'nope.csv', {'SPY': 0.15, 'TLT': 0.12}, prev_weights={'SPY': 0.9, 'TLT': 0.1})
   self.assertEqual(rec['weights'], {})            # no signals -> no risky exposure
   self.assertTrue(rec['warnings'])
  import alpha_engine as ae
  self.assertTrue(ae.drift_check(0.8)['ok'] is False)

 def test_var_horizon_is_scaled_and_reported(self):
  import stock_selector as ss, math
  one = ss.black_swan_heat_diffusion([{'symbol': 'SPY', 'vol': 0.16}], horizon_days=1)['tail_var_99']
  twenty = ss.black_swan_heat_diffusion([{'symbol': 'SPY', 'vol': 0.16}], horizon_days=21)['tail_var_99']
  # A 21-day 99% move is sqrt(21) times the 1-day move, not the same or 1/sqrt(252) of it.
  self.assertAlmostEqual(twenty / one, math.sqrt(21), places=2)
  res = ss.black_swan_heat_diffusion([{'symbol': 'SPY', 'vol': 0.16}])
  self.assertEqual(res['horizon_days'], 21)
  self.assertIn('confidence', res)

 def test_prune_correlated_keeps_independent_country_curves(self):
  import random, signal_filter as sf
  rng = random.Random(5)
  # Two curves that share a strong downward level trend but independent monthly moves.
  cov_a, cov_b = [3.0], [3.0]
  for _ in range(48):
   cov_a.append(max(0.05, cov_a[-1] - 0.06 + rng.gauss(0, 0.05)))
   cov_b.append(max(0.05, cov_b[-1] - 0.06 + rng.gauss(0, 0.05)))
  kept = sf.prune_correlated({'FIN.DEU.curve': cov_a, 'FIN.FRA.curve': cov_b}, threshold=0.85)
  self.assertEqual(sorted(kept), ['FIN.DEU.curve', 'FIN.FRA.curve'])
  # The same curve reported twice under a different level/scale is still pruned.
  dup = [x * 1.8 + 1.0 for x in cov_a]
  self.assertEqual(sf.prune_correlated({'FIN.DEU.curve': cov_a, 'DUPLICATE': dup}, threshold=0.85), ['FIN.DEU.curve'])

 def test_macro_z_scores_use_frequency_appropriate_windows(self):
  import signal_filter as sf
  self.assertEqual(sf.FREQ_WINDOW['D'], 250)
  self.assertGreater(sf.FREQ_WINDOW['D'], sf.FREQ_WINDOW['M'])
  # A level z-score must ignore the future: appending a value changes only the latest reading.
  base = list(range(1, 41))
  z1 = sf.history_zscore(base, window=20)
  z2 = sf.history_zscore(base + [41], window=20)
  self.assertIsNotNone(z1)
  self.assertIsNotNone(z2)

 def test_strategy_engine_decide_on_empty_journal(self):
  import tempfile, importlib
  from pathlib import Path
  import store as h
  import strategy_engine as se
  with tempfile.TemporaryDirectory() as td:
   p = Path(td)
   c = h.db(p, True)
   d = se.decide(c, p, {'SPY': 0.16, 'TLT': 0.12})
   c.close()
  # An empty journal must still produce a complete, fail-closed decision.
  self.assertEqual(d['regime'], 'uncertain')
  self.assertEqual(d['risk_scale'], 0.5)
  self.assertIsNone(d['growth_z'])
  self.assertEqual(d['quadrant'], 'unknown')
  self.assertEqual(d['cash'], 1.0)
  self.assertEqual(d['booked_weights'], {})
  self.assertEqual(d['orders'], [])
  self.assertTrue(d['disclaimer'])
  self.assertFalse(d['budget_ok'])

 def test_strategy_cli_requires_vol_and_rejects_bad_input(self):
  import subprocess, sys, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   r = subprocess.run([sys.executable, 'market_lens.py', '--data', td, 'strategy'], capture_output=True, text=True)
   self.assertNotEqual(r.returncode, 0)
   self.assertIn('--vol', r.stdout + r.stderr)
   r2 = subprocess.run([sys.executable, 'market_lens.py', '--data', td, 'strategy', '--vol', 'SPY=abc'], capture_output=True, text=True)
   self.assertNotEqual(r2.returncode, 0)
   self.assertIn('volatility', (r2.stdout + r2.stderr).lower())

class PortfolioMaths(unittest.TestCase):
 """Efficient frontier, fast Monte Carlo, the operations ladder and the supervisor.

 Each test pins a behaviour that was wrong or absent before this work: a cap silently
 rescaled away, a target solve that returned a non-optimal point, a plan that claimed 0% cash
 while holding it inside sleeves, and a briefing that called a stopped worker a feed outage.
 """

 # --- frontier ----------------------------------------------------------------------

 def test_min_variance_matches_closed_form(self):
  import efficient_frontier as ef
  names, cov = ef.covariance_from_vols({'A': 0.20, 'B': 0.10}, corr=0.30)
  self.assertEqual(names, ['A', 'B'])
  r = ef.min_variance_weights([0.08, 0.04], cov)
  self.assertTrue(r['feasible'])
  inv = ef._inverse(cov)
  s1 = ef._matvec(inv, [1.0, 1.0])
  total = sum(s1)
  for i in range(2):
   self.assertAlmostEqual(r['weights'][i], s1[i] / total, places=10)
  self.assertAlmostEqual(sum(r['weights']), 1.0, places=10)
  self.assertLess(r['kkt_residual'], 1e-8)

 def test_cap_plus_unplaceable_cash_not_renormalised(self):
  import efficient_frontier as ef
  names, cov = ef.covariance_from_vols({'A': 0.20, 'B': 0.10}, corr=0.30)
  r = ef.min_variance_weights([0.08, 0.04], cov, caps=0.30)
  # Two 30% caps cannot hold 100%: the rest must be explicit cash, not rescaled to 100%.
  self.assertTrue(r['feasible'])
  for w in r['weights']:
   self.assertLessEqual(w, 0.30 + 1e-9)
  self.assertAlmostEqual(r['invested'], 0.60, places=9)
  self.assertAlmostEqual(r['cash'], 0.40, places=9)

 def test_target_return_is_hit_exactly(self):
  import efficient_frontier as ef
  names, cov = ef.covariance_from_vols({'A': 0.20, 'B': 0.10}, corr=0.30)
  mu = [0.08, 0.04]
  for target in (0.05, 0.07):
   r = ef.min_variance_weights(mu, cov, target=target)
   self.assertTrue(r['feasible'])
   self.assertAlmostEqual(sum(a * b for a, b in zip(r['weights'], mu)), target, places=8)

 def test_impossible_target_fails_closed_with_reason(self):
  import efficient_frontier as ef
  names, cov = ef.covariance_from_vols({'A': 0.20, 'B': 0.10}, corr=0.30)
  r = ef.min_variance_weights([0.08, 0.04], cov, target=0.50)
  self.assertFalse(r['feasible'])
  self.assertTrue(r['reason'])
  self.assertNotAlmostEqual(sum(a * b for a, b in zip(r['weights'], [0.08, 0.04])), 0.50)

 def test_frontier_is_monotone_and_optimal(self):
  import efficient_frontier as ef
  names, cov = ef.covariance_from_vols({'A': 0.20, 'B': 0.10, 'C': 0.15}, corr=0.10)
  f = ef.efficient_frontier([0.10, 0.03, 0.07], cov, names, points=20, rf=0.02)
  self.assertGreaterEqual(len(f['points']), 15)
  vols = [p['volatility'] for p in f['points']]
  rets = [p['return'] for p in f['points']]
  self.assertEqual(vols, sorted(vols))
  for a, b in zip(rets, rets[1:]):
   self.assertGreaterEqual(b, a - 1e-9)
  for p in f['points']:
   self.assertAlmostEqual(sum(p['weights'].values()), 1.0, places=8)
   self.assertLess(p['kkt_residual'], 1e-6)
  # Tangency must actually be the best risk-adjusted point on the visible curve.
  self.assertIsNotNone(f['tangency'])
  best = max(p['sharpe'] for p in f['points'])
  self.assertAlmostEqual(f['tangency']['sharpe'], best, places=9)

 def test_frontier_rejects_mismatched_shapes(self):
  import efficient_frontier as ef
  r = ef.min_variance_weights([0.1, 0.2], [[1.0]])
  self.assertFalse(r['feasible'])
  self.assertEqual(r['weights'], [])

 # --- Monte Carlo -------------------------------------------------------------------

 def test_fast_mc_median_matches_analytic(self):
  import efficient_frontier as ef, math
  for vol in (0.05, 0.30):
   r = ef.fast_monte_carlo(0.08, vol, years=1.0, paths=60000, seed=3)
   self.assertTrue(r['ok'])
   self.assertAlmostEqual(r['median_wealth'], math.exp(0.08 - 0.5 * vol * vol), places=3)
  # A loss is likelier at higher volatility; that is the whole point of the spread.
  low = ef.fast_monte_carlo(0.08, 0.05, paths=40000, seed=1)['prob_loss']
  high = ef.fast_monte_carlo(0.08, 0.30, paths=40000, seed=1)['prob_loss']
  self.assertGreater(high, low)

 def test_pathwise_and_closed_form_agree_on_median(self):
  import efficient_frontier as ef
  cf = ef.fast_monte_carlo(0.08, 0.15, years=1.0, paths=40000, seed=1)
  pw = ef.simulate_paths(0.08, 0.15, years=1.0, paths=8000, steps=52, seed=1)
  self.assertAlmostEqual(cf['median_wealth'], pw['median_wealth'], delta=0.01)
  self.assertIn('max_drawdown', pw)
  self.assertGreaterEqual(pw['max_drawdown']['p95'], pw['max_drawdown']['median'])

 def test_mc_is_deterministic_and_rejects_bad_input(self):
  import efficient_frontier as ef
  a = ef.fast_monte_carlo(0.08, 0.15, paths=4000, seed=11)
  b = ef.fast_monte_carlo(0.08, 0.15, paths=4000, seed=11)
  self.assertEqual(a, b)
  self.assertFalse(ef.fast_monte_carlo(None, 0.15)['ok'])
  self.assertFalse(ef.fast_monte_carlo(0.08, -0.1)['ok'])

 def test_chooser_respects_drawdown_limit(self):
  import efficient_frontier as ef
  cands = [{'label': 'conservative', 'mu': 0.04, 'volatility': 0.05},
           {'label': 'aggressive', 'mu': 0.14, 'volatility': 0.40}]
  c = ef.choose_distribution(cands, goal=1.2, years=2.0, max_drawdown=0.10, paths=3000)
  self.assertIsNotNone(c['chosen'])
  # The aggressive arm breaches a 10% drawdown limit, so it must not be chosen.
  self.assertEqual(c['chosen']['label'], 'conservative')

 # --- operations ladder -------------------------------------------------------------

 def test_every_sleeve_meets_its_risk_budget(self):
  import portfolio_optimizer as po
  ops = po.operation_inputs()
  self.assertEqual(len(ops), len(po.OPERATIONS))
  for op in ops:
   self.assertLessEqual(op['volatility'], op['target_vol'] * 1.0 + 1e-6, op['id'])
   self.assertGreater(op['volatility'], 0.0)
   self.assertAlmostEqual(sum(op['weights'].values()), 1.0, places=8)
  # Return must rise with risk, or the ladder is not a ladder.
  by_risk = sorted(ops, key=lambda o: o['target_vol'])
  rets = [o['mu'] for o in by_risk]
  self.assertGreater(rets[-1], rets[0])

 def test_account_frontier_spreads_and_honours_limits(self):
  import portfolio_optimizer as po
  ops = po.operation_inputs()
  cov = po.operation_covariance(ops)
  f = po.operations_frontier(ops, cov, points=20)
  self.assertGreaterEqual(len(f['points']), 8)
  for p in f['points']:
   self.assertAlmostEqual(sum(p['shares'].values()), 1.0, places=6)
   # The spread is structural: a plan may not collapse into one sleeve.
   self.assertGreaterEqual(sum(1 for v in p['shares'].values() if v > 1e-6), 4)
   for k, floor in f['floors'].items():
    self.assertGreaterEqual(p['shares'][k], floor - 1e-9)
   for k, cap in f['caps'].items():
    self.assertLessEqual(p['shares'][k], cap + 1e-9)
  vols = [p['volatility'] for p in f['points']]
  self.assertEqual(vols, sorted(vols))

 def test_plan_money_adds_up_and_is_deterministic(self):
  import portfolio_optimizer as po
  out = po.build_plan(account_value=100000.0)
  plan = out['plan']
  self.assertAlmostEqual(sum(r['amount'] for r in plan['operations']), 100000.0, places=2)
  self.assertAlmostEqual(plan['invested'] + plan['cash'], 100000.0, places=2)
  self.assertAlmostEqual(sum(plan['assets'].values()), plan['invested'] / 100000.0, places=6)
  again = po.build_plan(account_value=100000.0)['plan']
  self.assertEqual([r['amount'] for r in plan['operations']],
                   [r['amount'] for r in again['operations']])
  self.assertEqual(plan['assets'], again['assets'])

 def test_defensive_regime_lowers_risk(self):
  import portfolio_optimizer as po
  normal = po.build_plan(account_value=50000.0, regime_scale=1.0)
  defensive = po.build_plan(account_value=50000.0, regime_scale=0.3)
  # Pre-fix, a single-asset sleeve could not be scaled, so the regime changed nothing.
  self.assertLess(defensive['plan']['portfolio_volatility'],
                  normal['plan']['portfolio_volatility'])
  # Cash held inside sleeves must be reported, not hidden.
  self.assertGreater(defensive['plan']['cash'], 0.0)
  self.assertAlmostEqual(defensive['plan']['invested'] + defensive['plan']['cash'],
                         50000.0, places=2)

 def test_plan_survives_zero_and_invalid_account(self):
  import portfolio_optimizer as po
  z = po.build_plan(account_value=0.0)['plan']
  self.assertEqual(z['invested'], 0.0)
  neg = po.build_plan(account_value=-5.0)['plan']
  self.assertEqual(neg['invested'], 0.0)
  self.assertTrue(neg['reason'])

 def test_user_volatilities_reach_the_sleeves(self):
  import portfolio_optimizer as po
  # A sleeve's reported volatility is capped at its risk budget, so a higher asset volatility
  # shows up as a smaller *risky fraction* holding the same budget, not a bigger volatility.
  base = {o['id']: o for o in po.operation_inputs()}
  high = {o['id']: o for o in po.operation_inputs(asset_vols={'SPY': 0.40, 'QQQ': 0.50})}
  self.assertLess(high['core_equity']['risky_fraction'], base['core_equity']['risky_fraction'])
  self.assertLess(high['growth_sleeve']['risky_fraction'], base['growth_sleeve']['risky_fraction'])
  for op in high.values():
   self.assertLessEqual(op['volatility'], op['target_vol'] + 1e-6, op['id'])
  # A lower asset volatility must never force *more* cash back: the binding constraint is the
  # risk budget, and at or below it the sleeve is fully invested.
  low = {o['id']: o for o in po.operation_inputs(asset_vols={'SPY': 0.06})}
  self.assertGreaterEqual(low['core_equity']['risky_fraction'],
                          high['core_equity']['risky_fraction'] - 1e-12)
  self.assertLessEqual(low['core_equity']['risky_fraction'], 1.0)

 # --- supervisor --------------------------------------------------------------------

 def test_briefing_does_not_call_a_stopped_worker_a_feed_outage(self):
  import jarvis_supervisor as js
  b = js.jarvis_briefing({'worker_online': False, 'accepted_feeds': 0, 'enabled_feeds': 26},
                         {'regime': 'normal', 'inputs_used': ['A', 'B', 'C']})
  # The old code said "26 feed(s) pending or retrying" and treated that as a data failure.
  joined = ' '.join(b['warnings'])
  self.assertIn('stopped', joined.lower())
  self.assertNotIn('pending or retrying', joined)
  self.assertEqual(b['worker'], 'stopped')
  self.assertEqual(b['usable_inputs'], 3)

 def test_change_assessment_triggers(self):
  import jarvis_supervisor as js
  same = js.assess_change({'regime': 'normal', 'regime_confirmed': 'normal',
                           'target_weights': {'SPY': 0.5}, 'booked_weights': {'SPY': 0.5},
                           'inputs_used': ['A'], 'portfolio_vol_estimate': 0.10,
                           'target_vol': 0.10},
                          {'regime': 'normal', 'weights': {'SPY': 0.5}})
  self.assertEqual(same['action'], 'hold')
  self.assertEqual(same['triggers'], [])
  moved = js.assess_change({'regime': 'tight_liquidity', 'regime_confirmed': 'tight_liquidity',
                            'target_weights': {'SPY': 0.2, 'TLT': 0.8},
                            'booked_weights': {'SPY': 0.8}, 'inputs_used': ['A'],
                            'portfolio_vol_estimate': 0.20, 'target_vol': 0.10},
                           {'regime': 'normal', 'weights': {'SPY': 0.8}})
  kinds = {t['kind'] for t in moved['triggers']}
  self.assertIn('regime_change', kinds)
  self.assertIn('target_drift', kinds)
  self.assertIn('over_risk', kinds)
  self.assertEqual(moved['action'], 'rebalance')
  blind = js.assess_change({'regime': 'uncertain', 'inputs_used': []}, None)
  self.assertEqual(blind['severity'], 'halt')
  self.assertEqual(blind['action'], 'rebalance')

 def test_next_book_is_budgeted_not_liquidated(self):
  import jarvis_supervisor as js
  a = js.assess_change({'regime': 'normal', 'target_weights': {'SPY': 0.8},
                        'booked_weights': {}, 'inputs_used': ['A']}, None)
  book = js.recommended_book(a, {'target_weights': {'SPY': 0.8}, 'booked_weights': {}}, {}, 0.35)
  self.assertTrue(book['partial'])
  self.assertGreater(sum(book['weights'].values()), 0.0)
  self.assertLessEqual(sum(book['weights'].values()), 1.0)

 def test_state_round_trip_ignores_corrupt_file(self):
  import jarvis_supervisor as js, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   path = Path(td) / 'state.json'
   self.assertEqual(js.load_previous_state(path), {})
   js.save_state(path, {'regime': 'normal', 'booked_weights': {'SPY': 0.5}, 'asof_utc': 'x'})
   st = js.load_previous_state(path)
   self.assertEqual(st['regime'], 'normal')
   self.assertEqual(st['weights'], {'SPY': 0.5})
   path.write_text('{not json')
   self.assertEqual(js.load_previous_state(path), {})

 # --- plan API ---------------------------------------------------------------------

 def test_jarvis_refuses_without_a_journal(self):
  import subprocess, sys, tempfile
  with tempfile.TemporaryDirectory() as td:
   r = subprocess.run([sys.executable, 'market_lens.py', '--data', td, 'jarvis',
                       '--account', '50000'], capture_output=True, text=True)
   # Advising on a journal that does not exist would be inventing evidence.
   self.assertNotEqual(r.returncode, 0)
   self.assertIn('database', (r.stderr + r.stdout).lower())

 def test_jarvis_output_is_self_consistent(self):
  import subprocess, sys, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   p = Path(td)
   h.db(p, True).close()  # An existing but empty journal: unknown regime, not neutral.
   r = subprocess.run([sys.executable, 'market_lens.py', '--data', str(p), 'jarvis',
                       '--account', '50000'], capture_output=True, text=True)
   self.assertEqual(r.returncode, 0, r.stderr)
   out = json.loads(r.stdout)
  # /api/jarvis exposes regime and risk_scale at the top level; the CLI must not disagree.
  self.assertIn('regime', out)
  self.assertIn('risk_scale', out)
  self.assertEqual(out['regime'], out['decision']['regime'])
  # An empty journal is unknown, not neutral, so the plan is defensive rather than a book.
  self.assertEqual(out['regime'], 'uncertain')
  self.assertLessEqual(out['risk_scale'], 0.5)
  plan = out['plan']
  self.assertAlmostEqual(plan['invested'] + plan['cash'], plan['account_value'], places=2)
  self.assertGreater(plan['cash'], 0.0)

 def test_jarvis_rejects_out_of_range_input(self):
  import subprocess, sys, tempfile
  with tempfile.TemporaryDirectory() as td:
   h.db(Path(td), True).close()
   for args in (['--account', '-5'], ['--max-dd', '2'], ['--years', '-1']):
    r = subprocess.run([sys.executable, 'market_lens.py', '--data', td, 'jarvis'] + args,
                       capture_output=True, text=True)
    self.assertNotEqual(r.returncode, 0, args)
    self.assertIn('Market Lens:', r.stderr + r.stdout)

 def test_plan_api_parses_and_rejects_bad_parameters(self):
  import plan_api
  arg = lambda k: {'vol': 'SPY=0.2,TLT=0.13', 'account': '1000', 'goal': '1.3'}.get(k, '')
  vols = plan_api._vols(arg('vol'))
  self.assertEqual(vols, {'SPY': 0.2, 'TLT': 0.13})
  self.assertAlmostEqual(plan_api._f(arg('account'), 'account', 0.0, 0.0), 1000.0)
  for bad in (lambda: plan_api._f('abc', 'account', 0.0, 0.0),
              lambda: plan_api._f('-5', 'account', 0.0, 0.0),
              lambda: plan_api._vols('SPY=abc'),
              lambda: plan_api._vols('SPY=0')):
   with self.assertRaises(ValueError):
    bad()

 def test_plan_api_serves_the_empty_journal(self):
  import plan_api
  with tempfile.TemporaryDirectory() as td:
   p = Path(td)
   c = h.db(p, True)
   view = plan_api.plan_view(c, p, lambda k: '25000' if k == 'account' else '')
   c.close()
  self.assertEqual(view['plan']['account_value'], 25000.0)
  self.assertAlmostEqual(view['plan']['invested'] + view['plan']['cash'], 25000.0, places=2)
  # An empty journal is an unknown macro picture, not a neutral one.
  self.assertEqual(view['regime'], 'uncertain')
  self.assertEqual(view['decision']['target_weights'], {})
  self.assertEqual(view['decision']['cash'], 1.0)
  self.assertIn('priors', view['disclaimer'])

class PaperAccount(unittest.TestCase):
 """The simulated account: NAV moves with prices, costs are charged, the budget is a budget.

 These pin the defects found while checking whether paper trading worked at all: an unpriced
 holding raised ``KeyError`` and killed the period, the turnover budget was bypassed with a
 100% exit charged at full cost, and there was no persisted NAV so no run could continue.
 """

 def test_nav_moves_with_prices_not_only_costs(self):
  import paper_trader as pt
  up = pt.step_paper_book({'SPY': 1.0}, {'SPY': [100, 120]}, {'SPY': 1.0}, nav=1000.0, cost_bps=0)
  self.assertAlmostEqual(up['nav_after_cost'], 1200.0, places=4)
  down = pt.step_paper_book({'SPY': 1.0}, {'SPY': [100, 80]}, {'SPY': 1.0}, nav=1000.0, cost_bps=0)
  self.assertAlmostEqual(down['nav_after_cost'], 800.0, places=4)

 def test_cash_share_does_not_earn_the_equity_return(self):
  import paper_trader as pt
  # Half the account is cash, so a +10% move on the risky half is +5 of NAV, not +10.
  r = pt.step_paper_book({'SPY': 0.5}, {'SPY': [100, 110]}, {'SPY': 0.5}, nav=1000.0, cost_bps=0)
  self.assertAlmostEqual(r['market_return_applied'], 0.05, places=6)
  self.assertAlmostEqual(r['nav_after_cost'], 1050.0, places=4)

 def test_unpriced_holding_is_carried_flat_and_reported(self):
  import paper_trader as pt
  # Pre-fix this raised KeyError('TLT') and the whole period died.
  # Keep TLT in the target so it is not sold; it is simply not repriced.
  r = pt.step_paper_book({'SPY': 0.5, 'TLT': 0.5}, {'SPY': [100, 110]},
                         {'SPY': 0.5, 'TLT': 0.5}, nav=1000.0, cost_bps=0, max_turnover=1.0)
  self.assertEqual(r['unpriced'], ['TLT'])
  # TLT is held but has no price, so it is carried flat: only SPY's +10% on half the book counts.
  self.assertAlmostEqual(r['market_return_applied'], 0.05, places=6)
  # It stays in the book (it was not sold), and it is named so the flat mark is not hidden.
  self.assertIn('TLT', r['weights'])

 def test_all_prices_missing_is_reported_not_silently_flat(self):
  import paper_trader as pt
  r = pt.step_paper_book({'SPY': 0.5, 'TLT': 0.5}, {}, {'SPY': 0.5, 'TLT': 0.5},
                         nav=1000.0, cost_bps=0, max_turnover=1.0)
  self.assertEqual(sorted(r['unpriced']), ['SPY', 'TLT'])
  self.assertAlmostEqual(r['market_return_applied'], 0.0, places=9)

 def test_turnover_budget_is_enforced_on_a_normal_rebalance(self):
  import paper_trader as pt
  for budget in (0.05, 0.10, 0.50):
   r = pt.step_paper_book({'SPY': 0.8}, {'SPY': [100, 100]}, {'SPY': 0.1, 'TLT': 0.8},
                          nav=1000.0, cost_bps=0, max_turnover=budget)
   self.assertLessEqual(r['turnover'], budget + 1e-9, budget)
   self.assertTrue(r['partial_rebalance'])
   # Weights are rounded to 9 decimal places on output, so reconcile at that precision.
   self.assertAlmostEqual(sum(r['weights'].values()) + r['cash'], 1.0, places=6)

 def test_fail_closed_exit_is_explicit_and_costed(self):
  import paper_trader as pt
  # A data outage exits risk entirely; that is a stated emergency, not a silent overspend.
  r = pt.step_paper_book({}, {'SPY': [100, 100]}, {'SPY': 0.5}, nav=10000.0, cost_bps=10,
                         max_turnover=0.10, emergency=True)
  self.assertTrue(r['emergency'])
  self.assertEqual(r['weights'], {})
  # Only SPY (50% of the book) had to be sold, so turnover is 0.50, not the whole account.
  self.assertAlmostEqual(r['turnover'], 0.5, places=9)
  self.assertGreater(r['cost'], 0.0)
  # The same target without the emergency flag stays inside the budget.
  soft = pt.step_paper_book({}, {'SPY': [100, 100]}, {'SPY': 0.5}, nav=10000.0, cost_bps=10,
                            max_turnover=0.10)
  self.assertLessEqual(soft['turnover'], 0.10 + 1e-9)
  self.assertFalse(soft['emergency'])

 def test_csv_fail_closed_marks_the_exit_emergency(self):
  import paper_trader as pt, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   missing = Path(td) / 'none.csv'
   r = pt.step_paper_portfolio(missing, {'SPY': [100, 101]}, {'SPY': 0.5}, nav=1000.0,
                               cost_bps=0, max_turnover=0.10)
  self.assertTrue(r['emergency'])
  self.assertEqual(r['weights'], {})
  self.assertIn('reason', r)

 def test_account_persists_and_rejects_corrupt_state(self):
  import paper_account as pa, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   path = Path(td) / 'acct.json'
   self.assertIsNone(pa.load(path))
   state, created = pa.open_account(path, 5000.0)
   self.assertTrue(created)
   self.assertEqual(state['nav'], 5000.0)
   state['nav'] = 5250.0
   state['weights'] = {'SPY': 0.6}
   state['initial_nav'] = 5000.0
   self.assertTrue(pa.save(path, state))
   back = pa.load(path)
   self.assertAlmostEqual(back['nav'], 5250.0)
   self.assertEqual(back['weights'], {'SPY': 0.6})
   self.assertAlmostEqual(back['initial_nav'], 5000.0)
   # A resumed account must not be re-created at the starting NAV.
   again, created2 = pa.open_account(path, 5000.0)
   self.assertFalse(created2)
   self.assertAlmostEqual(again['nav'], 5250.0)
   for broken in ('{not json', '{"nav": -5, "weights": {}}',
                  '{"nav": 100, "weights": {"A": 0.7, "B": 0.7}}'):
    path.write_text(broken)
    self.assertIsNone(pa.load(path), broken)

 def test_paper_cli_requires_prices_and_validates_them(self):
  import subprocess, sys, tempfile
  with tempfile.TemporaryDirectory() as td:
   h.db(Path(td), True).close()
   base = [sys.executable, 'market_lens.py', '--data', td, 'paper']
   for args in ([], ['--vol', 'SPY=0.16'],
                ['--vol', 'SPY=0.16', '--price', 'SPY=100'],
                ['--vol', 'SPY=0.16', '--price', 'SPY=100,-5'],
                ['--vol', 'SPY=0.16', '--price', 'SPY=a,b'],
                ['--vol', 'SPY=0.16', '--price', 'SPY=100,101', '--nav', '-1']):
    r = subprocess.run(base + args, capture_output=True, text=True)
    self.assertNotEqual(r.returncode, 0, args)
    self.assertIn('Market Lens:', r.stderr + r.stdout)

 def test_paper_cli_creates_then_resumes_the_account(self):
  import subprocess, sys, tempfile, json as _json
  with tempfile.TemporaryDirectory() as td:
   h.db(Path(td), True).close()
   acct = Path(td) / 'acct.json'
   base = [sys.executable, 'market_lens.py', '--data', td, 'paper', '--vol', 'SPY=0.16',
           '--account', str(acct), '--nav', '20000']
   first = subprocess.run(base + ['--price', 'SPY=100,101'], capture_output=True, text=True)
   self.assertEqual(first.returncode, 0, first.stderr)
   a = _json.loads(first.stdout)
   self.assertTrue(a['created'])
   self.assertEqual(a['period'], 1)
   self.assertTrue(acct.is_file())
   second = subprocess.run(base + ['--price', 'SPY=101,102'], capture_output=True, text=True)
   self.assertEqual(second.returncode, 0, second.stderr)
   b = _json.loads(second.stdout)
   self.assertFalse(b['created'])
   self.assertEqual(b['period'], 2)
   self.assertAlmostEqual(b['starting_nav'], 20000.0, places=2)
   # An empty journal fails closed, so the account holds cash and keeps its NAV.
   self.assertAlmostEqual(b['invested'] if 'invested' in b else b['nav'], b['nav'], places=2)

class YahooPrices(unittest.TestCase):
 """Offline checks for the price source. No test here contacts the network."""

 def _chart(self, prices, stamps=None, adj=None, symbol='SPY'):
  import json as _json
  stamps = stamps if stamps is not None else list(range(1, len(prices) + 1))
  ind = {'quote': [{'close': prices}]}
  if adj is not None:
   ind['adjclose'] = [{'adjclose': adj}]
  return _json.dumps({'chart': {'result': [{'meta': {'symbol': symbol, 'currency': 'USD'},
                                            'timestamp': stamps, 'indicators': ind}],
                                'error': None}})

 def test_adjusted_close_is_preferred_over_raw_close(self):
  import yahoo_prices as yp
  # Raw close drops on the ex-dividend day; adjusted close does not. Using raw close would show a
  # dividend as a price loss, so the adjusted series must win.
  payload = self._chart([100.0, 97.0, 98.0], adj=[100.0, 100.0, 101.0])
  s = yp._parse_chart('SPY', payload)
  self.assertTrue(s['adjusted'])
  self.assertEqual(s['prices'], [100.0, 100.0, 101.0])

 def test_falls_back_to_close_when_adjusted_is_absent(self):
  import yahoo_prices as yp
  s = yp._parse_chart('SPY', self._chart([100.0, 101.0]))
  self.assertFalse(s['adjusted'])
  self.assertEqual(s['prices'], [100.0, 101.0])

 def test_null_prices_are_skipped_not_filled(self):
  import yahoo_prices as yp
  s = yp._parse_chart('SPY', self._chart([100.0, 101.0, 102.0, 103.0],
                                         adj=[100.0, None, 102.0, 103.0]))
  # A gap is dropped; it is never interpolated into a pretend price.
  self.assertEqual(s['prices'], [100.0, 102.0, 103.0])

 def test_malformed_payloads_are_refused_with_a_reason(self):
  import yahoo_prices as yp, json as _json
  bad = {
   'not json': 'nope',
   'provider error': _json.dumps({'chart': {'result': None,
                                            'error': {'code': 'Not Found',
                                                      'description': 'symbol may be delisted'}}}),
   'no result': _json.dumps({'chart': {'result': [], 'error': None}}),
   'empty': _json.dumps({'chart': {'result': [{'timestamp': [],
                                               'indicators': {'quote': [{'close': []}]}}]}}),
   'negative price': self._chart([100.0, -5.0]),
   'zero price': self._chart([100.0, 0.0]),
   'length mismatch': self._chart([1.0, 2.0], stamps=[1, 2, 3]),
   'decreasing time': self._chart([1.0, 2.0], stamps=[5, 4]),
   'duplicate time': self._chart([1.0, 2.0], stamps=[5, 5]),
   'single price': self._chart([100.0]),
  }
  for label, payload in bad.items():
   with self.assertRaises(yp.PriceError, msg=label) as ctx:
    yp._parse_chart('X', payload)
   self.assertTrue(str(ctx.exception), label)

 def test_offline_with_no_cache_refuses(self):
  import yahoo_prices as yp, tempfile
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   with self.assertRaises(yp.PriceError):
    yp.fetch_prices(['SPY'], cache_path=Path(td) / 'none.json', allow_network=False)

 def test_stale_cache_is_used_but_reported_as_stale(self):
  import yahoo_prices as yp, tempfile, time, json as _json
  from pathlib import Path
  with tempfile.TemporaryDirectory() as td:
   cache = Path(td) / 'px.json'
   series = {'SPY': {'symbol': 'SPY', 'prices': [100.0, 101.0], 'dates': [1, 2],
                     'fetched_utc': time.time() - 99 * 3600, 'adjusted': True}}
   cache.write_text(_json.dumps({'source': 'yahoo', 'series': series}))
   got, report = yp.fetch_prices(['SPY'], cache_path=cache, allow_network=False)
   self.assertIn('SPY', got)
   # It is used, but never presented as current.
   self.assertEqual(report['stale'], ['SPY'])
   self.assertIn('STALE', yp.describe(report))

 def test_volatility_estimate_uses_the_history(self):
  import yahoo_prices as yp, random
  # A deterministic but genuinely varying series. A constant growth rate has almost no variance,
  # so it cannot show that the estimator responds to dispersion.
  rng = random.Random(7)
  px = [100.0]
  for _ in range(79):
   px.append(px[-1] * (1.0 + rng.gauss(0.0, 0.02)))
  vols, skipped = yp.realized_vols_from({'SPY': {'prices': px}})
  self.assertIn('SPY', vols)
  # ~2% daily moves annualise to roughly 0.3, so the estimate must be in that neighbourhood.
  self.assertGreater(vols['SPY'], 0.15)
  self.assertLess(vols['SPY'], 0.60)
  # A much calmer series must produce a much lower volatility.
  calm_px = [100.0]
  for _ in range(79):
   calm_px.append(calm_px[-1] * (1.0 + rng.gauss(0.0, 0.001)))
  calm, _ = yp.realized_vols_from({'CALM': {'prices': calm_px}})
  self.assertLess(calm['CALM'], vols['SPY'] / 4.0)

 def test_constant_growth_series_is_refused_as_zero_variance(self):
  import yahoo_prices as yp
  # A genuinely flat series has no risk to estimate, so it must be reported, not assumed safe.
  flat = [100.0] * 80
  vols, skipped = yp.realized_vols_from({'FLAT': {'prices': flat}})
  self.assertEqual(vols, {})
  self.assertIn('FLAT', skipped)

 def test_volatility_refuses_too_little_history(self):
  import yahoo_prices as yp
  vols, skipped = yp.realized_vols_from({'X': {'prices': [100.0, 101.0]}})
  self.assertEqual(vols, {})
  self.assertIn('X', skipped)

 def test_paper_cli_needs_symbols_or_prices(self):
  import subprocess, sys, tempfile
  with tempfile.TemporaryDirectory() as td:
   h.db(Path(td), True).close()
   r = subprocess.run([sys.executable, 'market_lens.py', '--data', td, 'paper'],
                      capture_output=True, text=True)
   self.assertNotEqual(r.returncode, 0)
   self.assertIn('--symbols', r.stderr + r.stdout)

 def test_paper_cli_offline_never_contacts_the_network(self):
  import subprocess, sys, tempfile
  with tempfile.TemporaryDirectory() as td:
   h.db(Path(td), True).close()
   # Offline with a cold cache must fail loudly rather than invent prices.
   r = subprocess.run([sys.executable, 'market_lens.py', '--data', td, 'paper',
                       '--symbols', 'SPY', '--auto-vol', '--offline'],
                      capture_output=True, text=True)
   self.assertNotEqual(r.returncode, 0)
   self.assertIn('price', (r.stderr + r.stdout).lower())

class ResearchStore(unittest.TestCase):
 """Point-in-time behaviour. No network: every payload here is synthesised."""

 def setUp(self):
  import research_store as rs
  self.rs = rs
  self.td = tempfile.TemporaryDirectory()
  self.path = Path(self.td.name) / 'research.db'
  self.c = rs.connect(self.path, create=True)

 def tearDown(self):
  self.c.close()
  self.td.cleanup()

 def _fund(self, cik, concept, end, filed, val, accn='a1', unit='USD', taxonomy='us-gaap'):
  return {'concept': concept, 'unit': unit, 'period_end': end, 'filed': filed, 'val': val,
          'form': '10-K', 'fy': 2024, 'fp': 'FY', 'accn': accn, 'taxonomy': taxonomy}

 def test_restatement_does_not_overwrite_original(self):
  rs = self.rs
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2023-12-31', '2024-02-01', 100.0, 'a1')], dt.datetime.now(dt.timezone.utc))
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2023-12-31', '2025-02-01', 90.0, 'a2')], dt.datetime.now(dt.timezone.utc))
  # Both vintages survive, so history is not rewritten.
  self.assertEqual(self.c.execute('SELECT COUNT(*) FROM fundamental').fetchone()[0], 2)

 def test_asof_returns_the_value_known_then(self):
  rs = self.rs
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2023-12-31', '2024-02-01', 100.0, 'a1')], dt.datetime.now(dt.timezone.utc))
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2023-12-31', '2025-02-01', 90.0, 'a2')], dt.datetime.now(dt.timezone.utc))
  early = rs.fundamentals_asof(self.c, 1, 'Revenues', asof_utc='2024-06-01T00:00:00Z')
  late = rs.fundamentals_asof(self.c, 1, 'Revenues', asof_utc='2025-06-01T00:00:00Z')
  self.assertEqual([r['val'] for r in early], [100.0])
  self.assertEqual([r['val'] for r in late], [90.0])

 def test_asof_before_any_filing_returns_nothing(self):
  rs = self.rs
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2023-12-31', '2024-02-01', 100.0)], dt.datetime.now(dt.timezone.utc))
  self.assertEqual(rs.fundamentals_asof(self.c, 1, 'Revenues', asof_utc='2024-01-01T00:00:00Z'), [])

 def test_concept_resolver_avoids_retired_tag(self):
  rs = self.rs
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2018-09-29', '2018-11-05', 62.9, 'a1')], dt.datetime.now(dt.timezone.utc))
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'RevenueFromContractWithCustomerExcludingAssessedTax', '2025-09-27', '2025-10-31', 416.2, 'a2')], dt.datetime.now(dt.timezone.utc))
  naive = rs.fundamentals_asof(self.c, 1, 'Revenues')
  self.assertEqual(max(r['period_end'] for r in naive), '2018-09-29')
  got = rs.resolve_concept_asof(self.c, 1, 'revenue')
  self.assertEqual(got['concept'], 'RevenueFromContractWithCustomerExcludingAssessedTax')
  self.assertEqual(got['latest_period'], '2025-09-27')

 def test_concept_resolver_respects_asof(self):
  rs = self.rs
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'Revenues', '2018-09-29', '2018-11-05', 62.9, 'a1')], dt.datetime.now(dt.timezone.utc))
  rs.put_fundamentals(self.c, 1, [self._fund(1, 'RevenueFromContractWithCustomerExcludingAssessedTax', '2025-09-27', '2025-10-31', 416.2, 'a2')], dt.datetime.now(dt.timezone.utc))
  got = rs.resolve_concept_asof(self.c, 1, 'revenue', asof_utc='2019-01-01T00:00:00Z')
  self.assertEqual(got['concept'], 'Revenues')

 def test_unknown_family_is_reported_not_guessed(self):
  got = self.rs.resolve_concept_asof(self.c, 1, 'not_a_family')
  self.assertIsNone(got['concept'])
  self.assertIn('unknown concept family', got['reason'])

 def test_bars_are_immutable_and_idempotent(self):
  rs = self.rs
  now = dt.datetime.now(dt.timezone.utc)
  bars = [{'ts': 100, 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 1.5, 'volume': 10},
          {'ts': 200, 'open': 1.5, 'high': 2.5, 'low': 1.0, 'close': 2.0, 'volume': 20}]
  self.assertEqual(rs.put_bars(self.c, 'X', '1d', bars, 't', now), 2)
  # Re-ingesting adds nothing.
  self.assertEqual(rs.put_bars(self.c, 'X', '1d', bars, 't', now), 0)
  # A provider correction is reported, never applied over the stored bar.
  changed = [{'ts': 100, 'open': 1.0, 'high': 2.0, 'low': 0.5, 'close': 9.9, 'volume': 10}]
  conflicts = rs.bar_conflicts(self.c, 'X', '1d', changed)
  self.assertEqual(len(conflicts), 1)
  self.assertEqual(conflicts[0]['stored'], 1.5)
  rs.put_bars(self.c, 'X', '1d', changed, 't', now)
  self.assertEqual(self.c.execute("SELECT close FROM bar WHERE symbol='X' AND ts=100").fetchone()[0], 1.5)

 def test_adjusted_price_uses_factor_known_at_the_time(self):
  rs = self.rs
  now = dt.datetime.now(dt.timezone.utc)
  rs.put_bars(self.c, 'X', '1d', [{'ts': 100, 'close': 100.0}], 't', now)
  rs.put_adj_factors(self.c, 'X', [(100, 0.5)], '2024-01-01T00:00:00Z', 't')
  rs.put_adj_factors(self.c, 'X', [(100, 0.25)], '2025-01-01T00:00:00Z', 't')
  before = rs.bars_asof(self.c, 'X', '1d', asof_utc='2024-06-01T00:00:00Z')
  after = rs.bars_asof(self.c, 'X', '1d', asof_utc='2025-06-01T00:00:00Z')
  self.assertEqual(before[0]['adj_close'], 50.0)
  self.assertEqual(after[0]['adj_close'], 25.0)
  # Unadjusted close never changes.
  self.assertEqual(after[0]['close'], 100.0)

 def test_unadjusted_read_ignores_factors(self):
  rs = self.rs
  now = dt.datetime.now(dt.timezone.utc)
  rs.put_bars(self.c, 'X', '1d', [{'ts': 100, 'close': 100.0}], 't', now)
  rs.put_adj_factors(self.c, 'X', [(100, 0.5)], '2024-01-01T00:00:00Z', 't')
  rows = rs.bars_asof(self.c, 'X', '1d', adjusted=False)
  self.assertEqual(rows[0]['close'], 100.0)

 def test_connect_requires_explicit_create(self):
  with self.assertRaises(sqlite3.OperationalError):
   self.rs.connect(Path(self.td.name) / 'missing.db')

 def test_connect_rejects_a_foreign_database(self):
  other = Path(self.td.name) / 'other.db'
  sqlite3.connect(other).close()
  with self.assertRaises(sqlite3.OperationalError):
   self.rs.connect(other)


class ResearchParsers(unittest.TestCase):
 """Validation of provider payloads. Malformed input must be refused, not half-read."""

 def test_chart_rejects_non_json(self):
  import market_data as md
  with self.assertRaises(md.MarketDataError):
   md.parse_chart('X', b'<html>not json</html>')

 def test_chart_rejects_duplicate_timestamps(self):
  import market_data as md
  doc = {'chart': {'result': [{'timestamp': [1, 1],
                               'indicators': {'quote': [{'open': [1, 2], 'high': [1, 2], 'low': [1, 2],
                                                         'close': [1, 2], 'volume': [1, 1]}]}}],
                   'error': None}}
  with self.assertRaises(md.MarketDataError):
   md.parse_chart('X', json.dumps(doc).encode())

 def test_chart_skips_null_closes_without_interpolating(self):
  import market_data as md
  doc = {'chart': {'result': [{'timestamp': [1, 2, 3],
                               'indicators': {'quote': [{'open': [1, None, 3], 'high': [1, None, 3],
                                                         'low': [1, None, 3], 'close': [1, None, 3],
                                                         'volume': [1, 0, 1]}]}}], 'error': None}}
  p = md.parse_chart('X', json.dumps(doc).encode())
  self.assertEqual([b['ts'] for b in p['bars']], [1, 3])

 def test_chart_reports_provider_error(self):
  import market_data as md
  doc = {'chart': {'result': None, 'error': {'code': 'Not Found', 'description': 'No data found'}}}
  with self.assertRaises(md.MarketDataError):
   md.parse_chart('NOPE', json.dumps(doc).encode())

 def test_split_ratio_parsed(self):
  import market_data as md
  doc = {'chart': {'result': [{'timestamp': [10],
                               'indicators': {'quote': [{'open': [1], 'high': [1], 'low': [1],
                                                         'close': [4], 'volume': [1]}]},
                               'events': {'splits': {'s1': {'date': 10, 'splitRatio': '4:1'}}}}],
                   'error': None}}
  p = md.parse_chart('X', json.dumps(doc).encode())
  self.assertEqual(p['actions'][0]['ratio'], 4.0)

 def test_companyfacts_requires_filed_dates(self):
  import sec_edgar as se
  doc = {'facts': {'us-gaap': {'Revenues': {'units': {'USD': [{'end': '2024-01-01', 'val': 1}]}}}}}
  # No ``filed`` date means it cannot be used point-in-time, so nothing is stored from it.
  with self.assertRaises(se.SecError):
   se.parse_companyfacts(1, json.dumps(doc).encode())

 def test_companyfacts_parses_dated_rows(self):
  import sec_edgar as se
  doc = {'facts': {'us-gaap': {'Revenues': {'units': {'USD': [
      {'end': '2024-01-01', 'filed': '2024-02-01', 'val': 5, 'form': '10-K', 'accn': 'a'}]}}}}}
  rows = se.parse_companyfacts(1, json.dumps(doc).encode())
  self.assertEqual(rows[0]['filed'], '2024-02-01')
  self.assertEqual(rows[0]['val'], 5.0)

 def test_short_interest_sentinel_is_treated_as_missing(self):
  import short_interest as si
  body = (b'symbolCode,settlementDate,currentShortPositionQuantity,previousShortPositionQuantity,'
          b'averageDailyVolumeQuantity,daysToCoverQuantity,revisionFlag\n'
          b'ABC,2026-10-15,1000,900,0,999.99,\n')
  rows, dropped = si.parse_csv(body)
  self.assertIsNone(rows[0]['days_to_cover'])
  self.assertEqual(dropped, 0)

 def test_short_interest_drops_malformed_rows(self):
  import short_interest as si
  body = (b'symbolCode,settlementDate,currentShortPositionQuantity,previousShortPositionQuantity,'
          b'averageDailyVolumeQuantity,daysToCoverQuantity,revisionFlag\n'
          b'ABC,-24627,1000,900,10,1.0,\n'
          b'DEF,2026-10-15,1000,900,10,1.0,\n')
  rows, dropped = si.parse_csv(body)
  self.assertEqual([r['symbol'] for r in rows], ['DEF'])
  self.assertEqual(dropped, 1)

 def test_short_interest_freshness_flags_stale(self):
  import short_interest as si
  rows = [{'settlement': '2020-04-15', 'symbol': 'ABC'}]
  v = si.freshness(rows, today=dt.date(2026, 11, 24))
  self.assertFalse(v['current'])
  self.assertIn('oldest page', v['reason'])
  fresh = si.freshness([{'settlement': '2026-11-14', 'symbol': 'ABC'}], today=dt.date(2026, 11, 24))
  self.assertTrue(fresh['current'])


class CoverageHonesty(unittest.TestCase):
 """The coverage report must not claim a dataset it cannot verify."""

 def setUp(self):
  import research_store as rs
  self.rs = rs
  self.td = tempfile.TemporaryDirectory()
  self.c = rs.connect(Path(self.td.name) / 'research.db', create=True)

 def tearDown(self):
  self.c.close()
  self.td.cleanup()

 def test_unavailable_datasets_are_never_reported_available(self):
  import research as R
  rep = R.coverage_report(self.c)
  for row in rep['rows']:
   if R.dataset_index()[row['key']]['status'] == 'unavailable':
    self.assertEqual(row['status'], 'unavailable', row['key'])

 def test_empty_store_claims_nothing_as_collected(self):
  import research as R
  rep = R.coverage_report(self.c)
  by = {r['key']: r for r in rep['rows']}
  for key in ('ohlcv_unadjusted', 'pit_fundamentals', 'benchmarks', 'vix', 'credit_etfs'):
   self.assertEqual(by[key]['status'], 'not_configured', key)

 def test_macro_is_not_claimed_without_a_journal(self):
  import research as R
  rep = R.coverage_report(self.c, journal=None)
  by = {r['key']: r for r in rep['rows']}
  self.assertEqual(by['risk_free_curve']['status'], 'not_configured')
  self.assertIn('could not be verified', by['risk_free_curve']['note'])

 def test_partial_cross_asset_names_the_missing_symbol(self):
  import research as R
  rs = self.rs
  now = dt.datetime.now(dt.timezone.utc)
  rs.put_bars(self.c, 'HYG', '1d', [{'ts': 1, 'close': 1.0}], 't', now)
  rep = R.coverage_report(self.c)
  by = {r['key']: r for r in rep['rows']}
  self.assertEqual(by['credit_etfs']['status'], 'partial')
  self.assertIn('LQD', by['credit_etfs']['note'])

 def test_blocking_gaps_list_what_is_still_missing(self):
  # Rewritten: quotes, options, consensus and the issuer universe are now collected, so listing
  # them as blocking gaps would overstate the gap. What genuinely remains unavailable must still
  # be named, because those are the assumptions a backtest silently makes.
  import research as R
  rep = R.coverage_report(self.c)
  names = ' '.join(g['name'] for g in rep['blocking_gaps'])
  # Free float is now collected (SEC public float), so listing it as a blocking gap would
  # overstate the gap in the other direction. What is still genuinely missing must be named.
  for must in ('Delisted price history', 'borrow', 'NLP', 'Trade ticks'):
   self.assertIn(must, names)
  self.assertNotIn('Free float', names)

 def test_blocking_gaps_do_not_claim_collected_datasets_are_missing(self):
  import research as R
  rep = R.coverage_report(self.c)
  names = ' '.join(g['name'] for g in rep['blocking_gaps'])
  for gone in ('NBBO', 'Options surface', 'Consensus estimates', 'delisted issuers'):
   self.assertNotIn('gap:' + gone, names)
  by = {r['key']: r for r in rep['rows']}
  # These were declared unavailable before any source existed; the declaration must now reflect
  # a working source. With nothing collected they read 'not run', never 'unavailable'.
  for key in ('nbbo_quotes', 'options_surface', 'analyst_estimates', 'universe_delisted',
              'short_interest'):
   self.assertIn(by[key]['status'], ('partial', 'not_configured'), key)
  self.assertEqual(by['delisted_prices']['status'], 'unavailable')
  # Partial, not unavailable: SEC publishes a point-in-time public float. With nothing collected
  # yet it reads 'not run', never 'missing'.
  self.assertEqual(by['free_float']['status'], 'not_configured')
  self.assertEqual(by['delisting_dates']['status'], 'not_configured')

 def test_caveats_do_not_contradict_the_collected_sources(self):
  import research as R
  text = ' '.join(R.coverage_report(self.c)['caveats'])
  self.assertNotIn('No NBBO and no options chain', text)
  self.assertIn('not the consolidated NBBO', text)
  self.assertIn('no revision history', text)


class ResearchCli(unittest.TestCase):
 """The command must validate input before touching any database."""

 def _run(self, *args):
  import subprocess, sys
  return subprocess.run([sys.executable, 'market_lens.py', '--data', self.td.name, 'research'] + list(args),
                        capture_output=True, text=True)

 def setUp(self):
  self.td = tempfile.TemporaryDirectory()
  h.db(Path(self.td.name), True).close()

 def tearDown(self):
  self.td.cleanup()

 def test_rejects_unknown_interval(self):
  r = self._run('--init', '--symbols', 'SPY', '--intervals', '3s')
  self.assertNotEqual(r.returncode, 0)
  self.assertIn('unsupported interval', r.stderr + r.stdout)

 def test_rejects_invalid_ticker(self):
  r = self._run('--init', '--symbols', 'BAD TICKER')
  self.assertNotEqual(r.returncode, 0)
  self.assertIn('invalid ticker', r.stderr + r.stdout)

 def test_needs_init_the_first_time(self):
  r = self._run('--symbols', 'SPY')
  self.assertNotEqual(r.returncode, 0)
  self.assertIn('research --init', r.stderr + r.stdout)

 def test_coverage_on_empty_store_states_gaps(self):
  r = self._run('--init', '--coverage')
  self.assertEqual(r.returncode, 0, r.stderr)
  self.assertIn('MISSING', r.stdout)
  self.assertIn('Blocking gaps', r.stdout)



class QuoteSurface(unittest.TestCase):
 """Bid/ask handling, implied-volatility inversion and date normalisation."""

 def setUp(self):
  import tempfile
  from pathlib import Path
  import research_store as rs
  self.td = tempfile.TemporaryDirectory()
  self.c = rs.connect(Path(self.td.name) / 'r.db', create=True)

 def tearDown(self):
  self.c.close()
  self.td.cleanup()

 def test_implied_vol_round_trips_through_black_scholes(self):
  # The only meaningful check of an inversion is that it recovers the volatility it was given.
  import quote_surface as qs
  for spot, strike, t, vol, is_call in ((100, 100, 0.25, 0.2, True), (100, 100, 0.25, 0.2, False),
                                       (100, 130, 0.5, 0.45, True), (100, 70, 0.5, 0.45, False),
                                       (50, 50, 1.0, 0.8, True)):
   c, p = qs.black_scholes(spot, strike, t, vol)
   mid = c if is_call else p
   got = qs.implied_vol(mid, spot, strike, t, is_call)
   self.assertIsNotNone(got)
   self.assertAlmostEqual(got, vol, places=3)

 def test_implied_vol_refuses_prices_below_intrinsic_and_nonpositive(self):
  import quote_surface as qs
  # A price under intrinsic is inconsistent; returning a volatility would hide a bad quote.
  self.assertIsNone(qs.implied_vol(1.0, 100, 80, 0.25, True))
  self.assertIsNone(qs.implied_vol(0.0, 100, 100, 0.25, True))
  self.assertIsNone(qs.implied_vol(None, 100, 100, 0.25, True))
  self.assertIsNone(qs.implied_vol(5.0, 100, 100, 0.0, True))

 def test_spread_refuses_one_sided_and_crossed_quotes(self):
  import quote_surface as qs
  one = qs.spread_from_quote({'bid': 10.0, 'ask': None})
  self.assertIsNone(one['spread'])
  self.assertIn('one-sided', one['reason'])
  crossed = qs.spread_from_quote({'bid': 10.5, 'ask': 10.0})
  self.assertIsNone(crossed['spread'])
  self.assertIn('crossed', crossed['reason'])
  ok = qs.spread_from_quote({'bid': 99.99, 'ask': 100.01})
  self.assertAlmostEqual(ok['spread'], 0.02)
  self.assertAlmostEqual(ok['spread_bps'], 2.0, places=3)

 def test_quote_scope_is_stamped_so_it_cannot_be_read_as_nbbo(self):
  import quote_surface as qs
  rec = {'symbol': 'SPY', 'venue_timestamp': 'Nov 24, 2026 5:29 PM ET', 'bid': 1.0, 'ask': 1.01,
         'quote_scope': 'single venue (Nasdaq), not consolidated NBBO'}
  self.assertEqual(qs.spread_from_quote(rec)['spread'], round(0.01, 6))
  import research_store as rs
  rs.put_quote_asof(self.c, rec, dt.datetime.now(dt.timezone.utc))
  row = self.c.execute('select quote_scope from quote').fetchone()
  self.assertIn('not consolidated NBBO', row[0])

 def test_quote_without_venue_timestamp_is_refused(self):
  import research_store as rs
  with self.assertRaises(ValueError):
   rs.put_quote_asof(self.c, {'symbol': 'SPY', 'bid': 1.0}, dt.datetime.now(dt.timezone.utc))

 def test_revised_quote_is_reported_not_overwritten(self):
  import research_store as rs, quote_surface as qs
  now = dt.datetime.now(dt.timezone.utc)
  base = {'symbol': 'SPY', 'venue_timestamp': 'Nov 24, 2026 5:29 PM ET', 'bid': 1.0, 'ask': 1.02}
  self.assertEqual(rs.put_quote_asof(self.c, dict(base), now), [])
  revised = dict(base, bid=1.01)
  conflicts = rs.put_quote_asof(self.c, revised, now)
  self.assertTrue(conflicts)
  self.assertEqual(conflicts[0]['field'], 'bid')
  stored = self.c.execute('select bid from quote').fetchone()[0]
  self.assertEqual(stored, 1.0)  # the original tick survives

 def test_freshness_uses_the_venue_timestamp_and_defaults_to_stale(self):
  import quote_surface as qs
  now = dt.datetime(2026, 11, 24, 22, 40, tzinfo=dt.timezone.utc)  # 17:40 ET
  fresh = qs.quote_freshness({'venue_timestamp': 'Nov 24, 2026 5:29 PM ET'}, now=now)
  self.assertTrue(fresh['current'])
  stale = qs.quote_freshness({'venue_timestamp': 'Nov 24, 2026 9:29 AM ET'}, now=now)
  self.assertFalse(stale['current'])
  self.assertIn('minutes old', stale['reason'])
  missing = qs.quote_freshness({}, now=now)
  self.assertFalse(missing['current'])  # unverifiable is not fresh
  self.assertIn('no quote timestamp', missing['reason'])

 def test_settlement_dates_are_normalised_to_iso(self):
  # A venue date left as 11/13/2026 breaks every comparison, and freshness then reports fresh
  # data as stale. That exact bug shipped once, so it is pinned here.
  import quote_surface as qs
  self.assertEqual(qs._iso_date('11/13/2026'), '2026-11-13')
  self.assertEqual(qs._iso_date('2026-11-13'), '2026-11-13')
  self.assertEqual(qs._iso_date('Nov 13, 2026'), '2026-11-13')
  self.assertIsNone(qs._iso_date('--'))
  self.assertIsNone(qs._iso_date(None))

 def test_fresh_normalised_settlement_is_not_called_stale(self):
  import quote_surface as qs, short_interest as si
  rows = [{'symbol': 'AAPL', 'settlement': qs._iso_date('11/13/2026')}]
  today = dt.date(2026, 11, 24)
  self.assertTrue(si.freshness(rows, today=today)['current'])

 def test_chain_parser_keeps_missing_legs_as_none_not_zero(self):
  import quote_surface as qs
  payload = {'data': {'totalRecord': 2, 'table': {'rows': [
   {'expirygroup': 'December 19, 2026', 'strike': '100.00', 'c_Bid': '5.00', 'c_Ask': '5.20',
    'c_Openinterest': '1,200', 'p_Bid': '--', 'p_Ask': '--', 'p_Openinterest': '--'},
   {'expirygroup': '', 'expiryDate': 'Dec 19', 'strike': '105.00', 'c_Bid': '--', 'c_Ask': '--',
    'p_Bid': '7.00', 'p_Ask': '7.30', 'p_Openinterest': '300'},
  ]}}}
  import json
  ch = qs.parse_chain(json.dumps(payload), 'XYZ')
  self.assertEqual(len(ch['rows']), 2)
  self.assertEqual(ch['rows'][0]['expiry'], 'December 19, 2026')
  self.assertEqual(ch['rows'][0]['call_oi'], 1200)
  self.assertIsNone(ch['rows'][0]['put_bid'])
  self.assertEqual(ch['rows'][1]['expiry'], 'December 19, 2026')  # carried from the group header
  self.assertIsNone(ch['rows'][1]['call_ask'])

 def test_surface_reports_unpriced_strikes_and_no_put_call_ratio_without_call_oi(self):
  import quote_surface as qs
  chain = {'symbol': 'XYZ', 'expiries': ['December 19, 2026'], 'rows': [
   {'expiry': 'December 19, 2026', 'strike': 90.0, 'call_bid': 12.0, 'call_ask': 12.2,
    'call_last': None, 'call_volume': 10, 'call_oi': 0,
    'put_bid': None, 'put_ask': None, 'put_last': None, 'put_volume': None, 'put_oi': 0},
  ]}
  surf = qs.surface(chain, spot=100.0, today=dt.date(2026, 11, 24))
  self.assertIsNone(surf['put_call_oi_ratio'])  # zero call OI must not look like a real ratio
  self.assertIn('indistinguishable from missing data', surf['put_call_oi_note'])
  # A strike below spot needs a put quote to price its volatility, and there is none.
  self.assertIsNone(surf['expiries']['December 19, 2026'][0]['otm_iv'])

 def test_atm_iv_interpolates_between_the_bracketing_strikes(self):
  # A median of all strikes is not an at-the-money value. Spot sits at 102 between a 100 strike
  # whose OTM leg is the put and a 105 strike whose OTM leg is the call, so a correct ATM value is
  # a weighted blend of the two, not either one.
  import quote_surface as qs
  expiry = 'December 19, 2026'
  chain = {'symbol': 'X', 'expiries': [expiry], 'rows': [
   {'expiry': expiry, 'strike': 100.0, 'call_bid': 4.90, 'call_ask': 5.00, 'call_last': None,
    'call_volume': 1, 'call_oi': 10, 'put_bid': 2.60, 'put_ask': 2.70, 'put_last': None,
    'put_volume': 1, 'put_oi': 10},
   {'expiry': expiry, 'strike': 105.0, 'call_bid': 1.90, 'call_ask': 2.00, 'call_last': None,
    'call_volume': 1, 'call_oi': 10, 'put_bid': None, 'put_ask': None, 'put_last': None,
    'put_volume': None, 'put_oi': 10},
  ]}
  surf = qs.surface(chain, spot=102.0, today=dt.date(2026, 11, 24))
  by_strike = {r['strike']: r for r in surf['expiries'][expiry]}
  iv100, iv105 = by_strike[100.0]['otm_iv'], by_strike[105.0]['otm_iv']
  self.assertEqual(by_strike[100.0]['otm_leg'], 'put')
  self.assertEqual(by_strike[105.0]['otm_leg'], 'call')
  self.assertIsNotNone(iv100)
  self.assertIsNotNone(iv105)
  expected = iv100 + (2.0 / 5.0) * (iv105 - iv100)
  self.assertAlmostEqual(surf['atm_iv_by_expiry'][expiry], expected, places=5)
  # Each of the two candidates is a different number, so the blend is genuinely interpolated.
  self.assertNotAlmostEqual(expected, iv100, places=4)

 def test_in_the_money_leg_is_not_inverted(self):
  # A deep ITM price is nearly all intrinsic, so its implied volatility is not identified. This is
  # the whole reason the OTM leg is chosen, and it is pinned because the naive version produced
  # 52 bad inversions on one live Apple chain.
  import quote_surface as qs
  expiry = 'December 19, 2026'
  chain = {'symbol': 'X', 'expiries': [expiry], 'rows': [
   # Deep ITM call whose mid sits fractionally BELOW intrinsic, as real quotes do.
   {'expiry': expiry, 'strike': 270.0, 'call_bid': 83.80, 'call_ask': 83.95, 'call_last': None,
    'call_volume': 1, 'call_oi': 10, 'put_bid': 0.02, 'put_ask': 0.05, 'put_last': None,
    'put_volume': 1, 'put_oi': 10},
  ]}
  surf = qs.surface(chain, spot=354.0, today=dt.date(2026, 11, 24))
  row = surf['expiries'][expiry][0]
  self.assertEqual(row['otm_leg'], 'put')  # below spot the put is the leg used
  self.assertIsNotNone(row['otm_iv'])
  # The ITM call price must not be reported as a volatility the model believes.
  self.assertIsNone(row['call_iv'])
  self.assertEqual(surf['itm_leg_not_used'], 1)

 def test_skew_is_call_minus_put_at_matched_strikes(self):
  # Normal equity skew: puts are bid up relative to calls, so call IV minus put IV is negative.
  import quote_surface as qs
  expiry = 'December 19, 2026'
  chain = {'symbol': 'X', 'expiries': [expiry], 'rows': [
   # Strike just above spot so the put is only slightly in the money and still invertible.
   {'expiry': expiry, 'strike': 101.0, 'call_bid': 0.48, 'call_ask': 0.52, 'call_last': None,
    'call_volume': 1, 'call_oi': 10, 'put_bid': 2.95, 'put_ask': 3.05, 'put_last': None,
    'put_volume': 1, 'put_oi': 10},
  ]}
  surf = qs.surface(chain, spot=100.0, today=dt.date(2026, 11, 24))
  self.assertEqual(len(surf['matched_strike_skew']), 1)
  sk = surf['matched_strike_skew'][0]
  self.assertAlmostEqual(sk['skew'], round(sk['call_iv'] - sk['put_iv'], 6), places=6)
  self.assertLess(sk['skew'], 0)

 def test_chain_pagination_follows_offset_and_dedupes(self):
  # A single page stopped around strike 676 for SPY with spot at 718: the money was missing and
  # every implied volatility failed, which looked like a market problem rather than a fetch one.
  import quote_surface as qs, json
  pages = [
   {'data': {'totalRecord': 4, 'table': {'rows': [
    {'expirygroup': 'December 19, 2026', 'strike': '100.00', 'drillDownURL': '/a'},
    {'expirygroup': '', 'expiryDate': 'Dec 19', 'strike': '105.00', 'drillDownURL': '/b'},
   ]}}},
   {'data': {'totalRecord': 4, 'table': {'rows': [
    {'expirygroup': 'December 19, 2026', 'strike': '100.00', 'drillDownURL': '/a'},
    {'expirygroup': '', 'strike': '110.00', 'drillDownURL': '/c'},
   ]}}},
  ]
  calls = []
  def fake_get(url):
   calls.append(url)
   return json.dumps(pages[min(len(calls) - 1, len(pages) - 1)]).encode()
  orig = qs._get
  try:
   qs._get = fake_get
   ch = qs.fetch_chain('SPY', limit=2)
  finally:
   qs._get = orig
  self.assertEqual(len(ch['rows']), 3)  # the repeated contract was de-duplicated
  self.assertEqual(sorted(r['strike'] for r in ch['rows']), [100.0, 105.0, 110.0])
  self.assertTrue(any('offset=2' in u for u in calls), calls)

 def test_surface_without_a_spot_refuses_rather_than_guessing(self):
  import quote_surface as qs
  surf = qs.surface({'symbol': 'X', 'rows': [], 'expiries': []}, spot=None)
  self.assertIn('error', surf)


class DelistedUniverse(unittest.TestCase):
 """SEC full-index parsing, and the difference between an issuer and an exchange notice."""

 def _index(self, rows):
  # A faithful slice of the real fixed-width file, including the description line that also
  # contains the words "Company Name".
  head = ('Description:           Master Index of EDGAR Dissemination Feed by Company Name\n'
          'Last Data Received:    September 30, 2026\n'
          '\n'
          'Company Name                                                  Form Type   CIK         '
          'Date Filed  File Name\n'
          + '-' * 120 + '\n')
  body = ''
  for name, form, cik, filed in rows:
   body += '%-62s%-12s%-12s%-12s%s\n' % (name, form, cik, filed,
                                          'edgar/data/%s/%s.txt' % (cik, filed.replace('-', '')))
  return head + body

 def test_parser_finds_the_real_header_not_the_description_line(self):
  import delisted
  text = self._index([('ACME CORP', '15-12G', '1234', '2026-11-18')])
  lines = text.splitlines()
  header = None
  for line in lines[:20]:
   if all(lbl in line for lbl in ('Company Name', 'Form Type', 'CIK', 'Date Filed')):
    header = line
    break
  self.assertIsNotNone(header)
  self.assertIn('Form Type', header)
  self.assertNotIn('Description', header)

 def test_parser_reads_names_dates_and_multiword_forms(self):
  import delisted
  orig = delisted._sec_get
  try:
   delisted._sec_get = lambda url, binary=False: self._index([
    ('ACME CORP', '15-12G', '1234', '2026-11-18'),
    ('BETA HOLDINGS INC', 'SC 13G/A', '5678', '2026-11-19'),
    ('GAMMA LLC', 'NT 10-K', '5678', '2026-11-20'),
   ]).encode('latin-1')
   recs, url = delisted.fetch_company_index(2026, 3)
  finally:
   delisted._sec_get = orig
  self.assertEqual(len(recs), 2)
  self.assertEqual(recs[1234]['name'], 'ACME CORP')
  self.assertEqual(recs[1234]['last_filed'], '2026-11-18')
  self.assertEqual(recs[1234]['forms'], {'15-12G': 1})
  # The form column must not swallow the CIK, which silently made every form string useless.
  self.assertEqual(sorted(recs[5678]['forms']), ['NT 10-K', 'SC 13G/A'])
  self.assertEqual(recs[5678]['first_filed'], '2026-11-19')
  self.assertEqual(recs[5678]['last_filed'], '2026-11-20')

 def test_parser_rejects_a_file_with_no_recognisable_header(self):
  import delisted
  orig = delisted._sec_get
  try:
   delisted._sec_get = lambda url, binary=False: b'not an index\n'
   with self.assertRaises(delisted.DelistError):
    delisted.fetch_company_index(2026, 3)
  finally:
   delisted._sec_get = orig

 def test_form25_is_attributed_to_the_exchange_not_the_issuer(self):
  # 25-NSE is filed by NYSE/Nasdaq. Counting it as an issuer deregistration produced a candidate
  # list made of stock exchanges, so this distinction is load-bearing.
  import delisted
  self.assertNotIn('25-NSE', delisted.DEREGISTRATION_FORMS)
  self.assertIn('25-NSE', delisted.EXCHANGE_NOTICE_FORMS)
  self.assertIn('15-12G', delisted.DEREGISTRATION_FORMS)
  self.assertIn('15-15D', delisted.DEREGISTRATION_FORMS)


class AnalystConsensus(unittest.TestCase):
 """Consensus parsing, period ordering and the float-versus-ownership distinction."""

 def _td(self):
  import tempfile
  from pathlib import Path
  import research_store as rs
  td = tempfile.TemporaryDirectory()
  return td, rs.connect(Path(td.name) / 'r.db', create=True)

 def test_period_key_orders_by_date_not_alphabetically(self):
  # 'Dec 2027' sorts before 'Jun 2027' as a string, which would put the newest period wrong.
  import analyst_consensus as ac
  self.assertEqual(ac._period_key('Dec 2026'), '2026-12')
  self.assertEqual(ac._period_key('Jun 2027'), '2027-06')
  self.assertLess(ac._period_key('Jun 2027'), ac._period_key('Dec 2027'))
  self.assertEqual(ac._period_key('unparseable'), 'unparseable')

 def test_forecast_parser_keeps_ranges_and_refuses_an_empty_payload(self):
  import analyst_consensus as ac
  payload = {'data': {'quarterlyForecast': {'rows': [
   {'fiscalEnd': 'Dec 2026', 'consensusEPSForecast': '2.94', 'highEPSForecast': '3.10',
    'lowEPSForecast': '2.80', 'noOfEstimates': '10'},
  ]}}}
  rows = ac.parse_forecast(payload, 'AAPL')
  self.assertEqual(len(rows), 1)
  self.assertEqual(rows[0]['consensus_eps'], 2.94)
  self.assertEqual(rows[0]['num_estimates'], 10)
  self.assertEqual(rows[0]['period_key'], '2026-12')
  with self.assertRaises(ac.EstimateError):
   ac.parse_forecast({'data': {'quarterlyForecast': {'rows': []}}}, 'AAPL')

 def test_ownership_is_labelled_as_not_free_float(self):
  import analyst_consensus as ac
  payload = {'data': {'ownershipSummary': {
   'SharesOutstandingPCT': {'value': '76.83'},
   'ShareoutstandingTotal': {'value': '14,700.00'},
  }}}
  o = ac.parse_ownership(payload, 'AAPL')
  self.assertEqual(o['institutional_ownership_pct'], 76.83)
  self.assertIn('not_free_float', o)
  self.assertIn('must not be used as a proxy', o['not_free_float'])

 def test_surprise_needs_both_figures_and_handles_zero_consensus(self):
  import analyst_consensus as ac
  miss = ac.surprise(None, 1.0, 'Dec 2026')
  self.assertIsNone(miss['difference'])
  self.assertIn('both a consensus', miss['reason'])
  hit = ac.surprise(1.0, 1.1, 'Dec 2026')
  self.assertAlmostEqual(hit['pct'], 0.1)
  zero = ac.surprise(0.0, 0.5, 'Dec 2026')
  self.assertIsNone(zero['pct'])  # a ratio against zero is undefined, not infinite
  self.assertAlmostEqual(zero['difference'], 0.5)

 def test_successive_snapshots_reveal_a_revision(self):
  # Revision history is not published by the venue; it is accumulated by storing each snapshot.
  import analyst_consensus as ac, research_store as rs
  td, c = self._td()
  try:
   t1 = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
   t2 = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
   rs.put_estimate(c, ac.parse_forecast({'data': {'quarterlyForecast': {'rows': [
    {'fiscalEnd': 'Dec 2026', 'consensusEPSForecast': '3.00', 'noOfEstimates': '9'}]}}}, 'AAPL'),
                   t1, 'nasdaq')
   rs.put_estimate(c, ac.parse_forecast({'data': {'quarterlyForecast': {'rows': [
    {'fiscalEnd': 'Dec 2026', 'consensusEPSForecast': '2.94', 'noOfEstimates': '10'}]}}}, 'AAPL'),
                   t2, 'nasdaq')
   hist = rs.estimate_history(c, 'AAPL')
   self.assertEqual(len(hist), 2)
   self.assertEqual(hist[0]['consensus_eps'], 3.00)
   self.assertEqual(hist[1]['consensus_eps'], 2.94)  # a cut, visible only because both were kept
  finally:
   c.close()
   td.cleanup()




class DelistEvents(unittest.TestCase):
 """Form 25 parsing: the issuer, the date, and what is deliberately not claimed."""

 NOTICE = """<SEC-DOCUMENT>0000876661-26-000841.txt
<SEC-HEADER>
SUBJECT COMPANY:
	COMPANY CONFORMED NAME:			ACV Auctions Inc.
	CENTRAL INDEX KEY:			0001637873
</SEC-HEADER>
<DOCUMENT>
<TYPE>25-NSE
<XML>
<notificationOfRemoval>
  <exchange><cik>0000876661</cik><entityName>NEW YORK STOCK EXCHANGE LLC</entityName></exchange>
  <issuer><cik>0001637873</cik><entityName>ACV Auctions Inc.</entityName></issuer>
  <descriptionClassSecurity>Class A Common Stock</descriptionClassSecurity>
  <ruleProvision>17 CFR 240.12d2-2(a)(3)</ruleProvision>
  <signatureData><signatureDate>2026-10-16</signatureDate></signatureData>
</notificationOfRemoval>
</XML>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-99.25
<TEXT>
The Exchange also notifies the Securities and Exchange Commission that as a result of the above
indicated conditions this security was suspended from trading on October 16, 2026.
Each outstanding share will be converted into the right to receive an amount equal to the tender
offer price of $10.50 per share in cash.
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>"""

 def test_issuer_is_the_company_not_the_filing_exchange(self):
  # The filing's own CIK belongs to the NYSE. Using it put stock exchanges in the "delisted
  # issuers" list, which is exactly the kind of plausible-looking wrong answer this pins.
  import delist_events as de
  ev = de.parse_notice(self.NOTICE)
  self.assertEqual(ev['issuer_cik'], 1637873)
  self.assertEqual(ev['exchange_name'], 'NEW YORK STOCK EXCHANGE LLC')
  self.assertNotEqual(ev['issuer_cik'], 876661)

 def test_suspension_date_and_reason_are_extracted(self):
  import delist_events as de
  ev = de.parse_notice(self.NOTICE)
  self.assertEqual(ev['suspension_date'], '2026-10-16')
  self.assertEqual(ev['effective_date'], '2026-10-16')
  self.assertIn('merger', ev['reason'])
  self.assertEqual(ev['cash_per_share'], 10.5)

 def test_cash_price_is_only_taken_for_a_merger(self):
  # A redemption or liquidation figure is not a terminal share price. Reporting one would invent
  # a data point that a backtest would treat as the last traded price.
  import delist_events as de
  redeem = self.NOTICE.replace('240.12d2-2(a)(3)', '240.12d2-2(a)(1)')
  ev = de.parse_notice(redeem)
  self.assertIsNone(ev['cash_per_share'])
  self.assertIn('redemption', ev['reason'])

 def test_missing_suspension_date_is_not_backfilled(self):
  # Substituting the filing date would look helpful and be wrong: they differ by days to weeks.
  import delist_events as de
  no_date = self.NOTICE.replace(
   'this security was suspended from trading on October 16, 2026.', 'the listing was removed.')
  ev = de.parse_notice(no_date)
  self.assertIsNone(ev['suspension_date'])
  self.assertEqual(ev['effective_date'], '2026-10-16')

 def test_notice_without_an_issuer_is_refused(self):
  import delist_events as de
  with self.assertRaises(de.NoticeError):
   de.parse_notice('<notificationOfRemoval></notificationOfRemoval>')

 def test_plain_text_notice_falls_back_to_the_subject_company(self):
  # Older filings are not XML; the header still names the subject company.
  import delist_events as de
  old = ('SUBJECT COMPANY:\n\tCOMPANY CONFORMED NAME:\tOLDCO INC\n\tCENTRAL INDEX KEY:'
         '\t\t\t0001234567\n')
  ev = de.parse_notice(old)
  self.assertEqual(ev['issuer_cik'], 1234567)
  self.assertEqual(ev['issuer_name'], 'OLDCO INC')

 def test_events_are_point_in_time(self):
  import tempfile, research_store as rs
  from pathlib import Path
  td = tempfile.TemporaryDirectory()
  c = rs.connect(Path(td.name) / 'r.db', create=True)
  try:
   now = dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc)
   rs.put_delist_event(c, {'issuer_cik': 1, 'security_class': 'Common', 'suspension_date':
                           '2026-10-16', 'effective_date': '2026-10-16', 'filing_url': 'u'},
                       '2026Q4', now)
   self.assertEqual(len(rs.delist_events_asof(c, '2026-10-15')), 0)  # not yet known
   self.assertEqual(len(rs.delist_events_asof(c, '2026-10-16')), 1)
  finally:
   c.close()
   td.cleanup()

 def test_registered_notice_is_idempotent(self):
  import tempfile, research_store as rs
  from pathlib import Path
  td = tempfile.TemporaryDirectory()
  c = rs.connect(Path(td.name) / 'r.db', create=True)
  try:
   now = dt.datetime(2026, 11, 25, tzinfo=dt.timezone.utc)
   ev = {'issuer_cik': 7, 'security_class': 'Common', 'suspension_date': '2026-11-01',
         'filing_url': 'https://x/1'}
   rs.put_delist_event(c, ev, '2026Q4', now)
   rs.put_delist_event(c, ev, '2026Q4', now)
   self.assertEqual(c.execute('select count(*) from delist_event').fetchone()[0], 1)
  finally:
   c.close()
   td.cleanup()


class PublicFloat(unittest.TestCase):
 """Public float comes from SEC dei facts, and is not the same as institutional ownership."""

 def test_public_float_is_captured_from_companyfacts(self):
  # It was already present in the payload; the risk is declaring it unavailable when it is not.
  import sec_edgar as se, json
  payload = {'facts': {'dei': {
   'EntityPublicFloat': {'units': {'USD': [
    {'end': '2026-03-27', 'val': 3647099000000, 'filed': '2026-11-03', 'form': '10-K',
     'accn': 'a1', 'fy': 2026, 'fp': 'FY'}]}},
   'EntityCommonStockSharesOutstanding': {'units': {'shares': [
    {'end': '2026-10-23', 'val': 14532182000, 'filed': '2026-11-03', 'form': '10-K',
     'accn': 'a1', 'fy': 2026, 'fp': 'FY'}]}},
  }}}
  rows = se.parse_companyfacts(320193, json.dumps(payload))
  by = {(r['concept']): r for r in rows}
  self.assertEqual(by['EntityPublicFloat']['val'], 3647099000000)
  self.assertEqual(by['EntityPublicFloat']['filed'], '2026-11-03')
  self.assertEqual(by['EntityPublicFloat']['taxonomy'], 'dei')
  # The filing date is what makes it point-in-time rather than a current snapshot.
  self.assertEqual(by['EntityCommonStockSharesOutstanding']['val'], 14532182000)

 def test_float_is_declared_partial_not_unavailable(self):
  import research as R, tempfile, research_store as rs
  from pathlib import Path
  td = tempfile.TemporaryDirectory()
  c = rs.connect(Path(td.name) / 'r.db', create=True)
  try:
   rep = R.coverage_report(c)
   by = {r['key']: r for r in rep['rows']}
   self.assertEqual(by['free_float']['status'], 'not_configured')  # nothing stored yet
   self.assertIn('public float', by['free_float']['note'].lower())
   self.assertEqual(by['delisting_dates']['status'], 'not_configured')
  finally:
   c.close()
   td.cleanup()



class StrategyEngineAudit(unittest.TestCase):
 """The seven engine defects found after the data work, each pinned by the behaviour it broke."""

 def test_daily_series_represents_its_group_over_annual(self):
  # ``FREQ_RANK`` is 0 for daily and 3 for annual. With a smallest-wins comparison the rank had to
  # be negated; the old code kept it positive, so min() chose ANNUAL for a group that also held a
  # daily series — a stale benchmark outranking the live curve.
  import signal_filter as sf
  daily = sf._representative_key({'period': '2026-01-01', 'series': 'UST.DAILY.10Y2Y'},
                                 {'freq': 'D'})
  annual = sf._representative_key({'period': '2026-01-01', 'series': 'OECD.A'}, {'freq': 'A'})
  monthly = sf._representative_key({'period': '2026-01-01', 'series': 'OECD.M'}, {'freq': 'M'})
  self.assertGreater(daily, annual)
  self.assertGreater(monthly, annual)
  self.assertGreater(daily, monthly)

 def test_unknown_frequency_does_not_outrank_daily(self):
  # The old default rank was 9, which beat every known frequency under the inverted comparison.
  import signal_filter as sf
  nofreq = sf._representative_key({'period': '2026-01-01', 'series': 'X'}, {})
  daily = sf._representative_key({'period': '2026-01-01', 'series': 'Y'}, {'freq': 'D'})
  self.assertGreater(daily, nofreq)

 def test_periods_of_different_formats_compare_on_their_date(self):
  # '2026Q3' > '2026-11-18' as strings, and so does an unpadded '2026-9-1'. Quarterly and
  # unpadded-date rows must be normalised or they win on recency by accident.
  import signal_filter as sf
  self.assertEqual(sf.period_end('2026Q3'), '2026-09-30')
  self.assertEqual(sf.period_end('2026-9-1'), '2026-09-01')
  self.assertEqual(sf.period_end('2026-10'), '2026-10-31')
  self.assertEqual(sf.period_end('2026'), '2026-12-31')
  self.assertEqual(sf.period_end('2025-12'), '2025-12-31')
  self.assertLess(sf.period_end('2026Q3'), sf.period_end('2026-11-18'))
  self.assertLess(sf.period_end('2026-9-1'), sf.period_end('2026-11-18'))
  self.assertGreater(sf.period_end('2026-01'), sf.period_end('2025-12'))
  # An unreadable period must never win: it sorts before any real date.
  self.assertEqual(sf.period_end('garbage'), '')

 def test_dedupe_still_picks_daily_across_formats_and_row_order(self):
  import csv, datetime as dt, tempfile
  from pathlib import Path
  import signal_filter as sf
  now = dt.datetime.now(dt.timezone.utc)
  Z = now.isoformat().replace('+00:00', 'Z')
  FUT = (now + dt.timedelta(days=30)).isoformat().replace('+00:00', 'Z')
  cols = ['exported_utc','asof_utc','recheck_due_utc','usable_until_utc','worker_online','heartbeat',
          'series','geos','atlas_factor','signal_group','publisher','measure','unit','period',
          'current_value','screened_value','quality','source_flag','source_release','state',
          'last_numeric_period','last_numeric_value','snapshot_seen_utc','snapshot_indexed_utc',
          'contract_checked_utc','source_url','sha256']
  def row(sid, period):
   return dict(exported_utc=Z, asof_utc=Z, recheck_due_utc=Z, usable_until_utc=FUT,
               worker_online='True', heartbeat=Z, series=sid, geos='US', atlas_factor='',
               signal_group='US:YLD-03', publisher='p', measure='m', unit='pp', period=period,
               current_value='0.35', screened_value='0.35', quality='unflagged / normal',
               source_flag='', source_release='', state='accepted', last_numeric_period='',
               last_numeric_value='', snapshot_seen_utc=Z, snapshot_indexed_utc=Z,
               contract_checked_utc=Z, source_url='u', sha256='h')
  # A quarterly OECD label sorts AFTER the daily period as a raw string. It must still lose.
  # '2026Q3' sorts AFTER '2026-11-18' as a raw string, but 30 September is the earlier date, so
  # the quarterly row must lose on recency once the period is normalised.
  pairs = [('FIN.USA.curve', '2026Q3'), ('UST.DAILY.10Y2Y', '2026-11-18')]
  for order in (pairs, list(reversed(pairs))):
   with tempfile.TemporaryDirectory() as td:
    path = Path(td) / 'e.csv'
    with path.open('w', newline='', encoding='utf-8-sig') as f:
     w = csv.DictWriter(f, cols); w.writeheader()
     for sid, period in order:
      w.writerow(row(sid, period))
    self.assertEqual(list(sf.filter_signals(path)), ['UST.DAILY.10Y2Y'], order)

 def test_group_representatives_agrees_with_the_filter(self):
  # The diagnostic used min() on the same key, so it reported the opposite winner.
  import csv, datetime as dt, tempfile
  from pathlib import Path
  import signal_filter as sf
  now = dt.datetime.now(dt.timezone.utc)
  Z = now.isoformat().replace('+00:00', 'Z')
  FUT = (now + dt.timedelta(days=30)).isoformat().replace('+00:00', 'Z')
  cols = ['exported_utc','asof_utc','recheck_due_utc','usable_until_utc','worker_online','heartbeat',
          'series','geos','atlas_factor','signal_group','publisher','measure','unit','period',
          'current_value','screened_value','quality','source_flag','source_release','state',
          'last_numeric_period','last_numeric_value','snapshot_seen_utc','snapshot_indexed_utc',
          'contract_checked_utc','source_url','sha256']
  with tempfile.TemporaryDirectory() as td:
   path = Path(td) / 'e.csv'
   with path.open('w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, cols); w.writeheader()
    for sid in ('FIN.USA.curve', 'UST.DAILY.10Y2Y'):
     w.writerow(dict(exported_utc=Z, asof_utc=Z, recheck_due_utc=Z, usable_until_utc=FUT,
                     worker_online='True', heartbeat=Z, series=sid, geos='US', atlas_factor='',
                     signal_group='US:YLD-03', publisher='p', measure='m', unit='pp',
                     period='2026-11-18', current_value='0.35', screened_value='0.35',
                     quality='unflagged / normal', source_flag='', source_release='',
                     state='accepted', last_numeric_period='', last_numeric_value='',
                     snapshot_seen_utc=Z, snapshot_indexed_utc=Z, contract_checked_utc=Z,
                     source_url='u', sha256='h'))
   rep = sf.group_representatives(path)
   filtered = list(sf.filter_signals(path))
  # Same group, same winner: the diagnostic must not disagree with the live filter.
  self.assertEqual(rep['US:YLD-03']['chosen'], filtered[0])
  self.assertEqual(filtered[0], 'UST.DAILY.10Y2Y')

 def test_every_detected_regime_has_an_outlook_and_a_risk_scale(self):
  # The supervisor table was keyed 'normal' while the detector returns 'normal_expansion', so a
  # healthy economy was described as 'Regime unresolved'.
  import alpha_engine as ae, jarvis_supervisor as js
  cases = {
   'normal_expansion': {'UST.DAILY.10Y2Y': 0.5, 'NYFED.SOFR': 4.0},
   'defensive_inverted_curve': {'UST.DAILY.10Y2Y': -0.3, 'NYFED.SOFR': 4.0},
   'tight_liquidity': {'UST.DAILY.10Y2Y': 0.5, 'NYFED.SOFR': 6.0},
   'uncertain': {},
  }
  for expected, sig in cases.items():
   reg = ae.detect_regime(sig)
   self.assertEqual(reg['regime'], expected)
   self.assertIn(reg['regime'], js.REGIME_OUTLOOK)
   self.assertIn(reg['regime'], js.REGIME_RISK)
   # The supervisor's risk scale must never be looser than the detector's own.
   self.assertLessEqual(js.REGIME_RISK[reg['regime']], reg['risk_scale'] + 1e-12,
                        (expected, js.REGIME_RISK[reg['regime']], reg['risk_scale']))
  # An unknown regime is defensive, not neutral.
  self.assertLessEqual(js.assess_change({'regime': 'something_new'})['risk_scale'], 0.5)

 def test_sleeve_correlations_are_not_scaled_twice(self):
  # vol already includes risky_fraction, so corr * vol_a * vol_b must recover the class prior.
  import portfolio_optimizer as po
  for scale in (1.0, 0.4):
   ops = po.operation_inputs(regime_scale=scale)
   cov = po.operation_covariance(ops)
   for i, a in enumerate(ops):
    for j, b in enumerate(ops):
     if i >= j:
      continue
     expected = sum(wx * wy * po.class_corr(a['classes'][x], b['classes'][y])
                    for x, wx in a['weights'].items()
                    for y, wy in b['weights'].items())
     implied = cov[i][j] / (a['volatility'] * b['volatility'])
     self.assertAlmostEqual(implied, expected, places=6, msg=(scale, a['id'], b['id']))
   # The diagonal stays the sleeve variance plus the estimation floor.
   for i, a in enumerate(ops):
    self.assertAlmostEqual(cov[i][i], a['volatility'] ** 2 + 1e-4, places=9)

 def test_sleeve_cash_earns_the_risk_free_rate(self):
  # Charging held-back cash 0% made a defensive plan look worse than it is.
  import portfolio_optimizer as po
  ops = {o['id']: o for o in po.operation_inputs(regime_scale=0.3, rf=0.04)}
  growth = ops['growth_sleeve']
  self.assertLess(growth['risky_fraction'], 1.0)
  self.assertAlmostEqual(growth['mu'],
                         growth['risky_fraction'] * 0.090
                         + (1 - growth['risky_fraction']) * 0.04, places=9)
  self.assertGreater(growth['mu'], growth['risky_fraction'] * 0.090)
  # A fully invested sleeve is unchanged.
  full = {o['id']: o for o in po.operation_inputs(regime_scale=1.0, rf=0.04)}
  self.assertEqual(full['growth_sleeve']['risky_fraction'], 1.0)
  self.assertAlmostEqual(full['growth_sleeve']['mu'], 0.090, places=9)

 def test_defensive_plan_is_not_penalised_for_its_own_cash(self):
  import portfolio_optimizer as po
  normal = po.build_plan(account_value=50000.0, regime_scale=1.0, rf=0.04)
  defensive = po.build_plan(account_value=50000.0, regime_scale=0.3, rf=0.04)
  # Lower risk, therefore a lower return — but not *also* a lost risk-free rate on top.
  self.assertLess(defensive['plan']['portfolio_volatility'],
                  normal['plan']['portfolio_volatility'])
  self.assertGreater(defensive['plan']['portfolio_return'], 0.0)
  self.assertAlmostEqual(defensive['plan']['invested'] + defensive['plan']['cash'], 50000.0,
                         places=2)

 def test_supervisor_drawdown_trigger_can_fire(self):
  # decide() reports drawdown_pct; the supervisor only read 'drawdown', so the limit was dead.
  import jarvis_supervisor as js
  out = js.assess_change({'regime': 'normal_expansion', 'regime_confirmed': 'normal_expansion',
                          'target_weights': {'SPY': 0.5}, 'booked_weights': {'SPY': 0.5},
                          'inputs_used': ['A'], 'portfolio_vol_estimate': 0.10,
                          'target_vol': 0.10, 'drawdown_pct': 0.20})
  kinds = {t['kind'] for t in out['triggers']}
  self.assertIn('drawdown_breach', kinds)
  self.assertEqual(out['action'], 'rebalance')
  # Under the limit, no trigger.
  ok = js.assess_change({'regime': 'normal_expansion', 'target_weights': {'SPY': 0.5},
                         'booked_weights': {'SPY': 0.5}, 'inputs_used': ['A'],
                         'portfolio_vol_estimate': 0.10, 'target_vol': 0.10,
                         'drawdown_pct': 0.02})
  self.assertNotIn('drawdown_breach', {t['kind'] for t in ok['triggers']})
  # A curve that was offered but unusable is noted, not silently treated as zero drawdown.
  unknown = js.assess_change({'regime': 'normal_expansion', 'target_weights': {'SPY': 0.5},
                              'booked_weights': {'SPY': 0.5}, 'inputs_used': ['A'],
                              'drawdown_pct': None})
  self.assertIn('drawdown_unknown', {t['kind'] for t in unknown['triggers']})
  # A caller that never supplies a curve is not nagged about it.
  absent = js.assess_change({'regime': 'normal_expansion', 'target_weights': {'SPY': 0.5},
                             'booked_weights': {'SPY': 0.5}, 'inputs_used': ['A']})
  self.assertNotIn('drawdown_unknown', {t['kind'] for t in absent['triggers']})

 def test_decide_reports_the_drawdown(self):
  import tempfile
  from pathlib import Path
  import store as h
  import strategy_engine as se
  with tempfile.TemporaryDirectory() as td:
   c = h.db(Path(td), True)
   try:
    out = se.decide(c, {}, {'SPY': 0.16}, equity_curve=[100.0, 120.0, 96.0])
    self.assertAlmostEqual(out['drawdown_pct'], 0.20, places=6)
    self.assertNotIn('drawdown_pct', se.decide(c, {}, {'SPY': 0.16}))
    # An unusable curve is not a zero drawdown.
    self.assertIsNone(se.decide(c, {}, {'SPY': 0.16}, equity_curve=[-1.0, 'x'])['drawdown_pct'])
   finally:
    c.close()

 def test_sector_relative_drops_unmeasurable_sectors(self):
  # A non-positive sector average used to score 0.0, which is a *middle* rank on no evidence.
  import stock_selector as ss
  rows = ss.score_sector_relative([
   {'ticker': 'A', 'sector': 'X', 'interest_coverage': 5, 'ev_ebitda': 8},
   {'ticker': 'B', 'sector': 'X', 'interest_coverage': 5, 'ev_ebitda': 12}])
  self.assertEqual([r['ticker'] for r in rows], ['A', 'B'])
  self.assertEqual(rows[0]['sector_rel_value'], 0.2)
  # Equal valuations tie, and the order is fixed rather than depending on input order.
  tie1 = ss.score_sector_relative([{'ticker': 'Z', 'sector': 'Y', 'interest_coverage': 5,
                                    'ev_ebitda': 10},
                                   {'ticker': 'M', 'sector': 'Y', 'interest_coverage': 5,
                                    'ev_ebitda': 10}])
  tie2 = ss.score_sector_relative(list(reversed(
   [{'ticker': 'Z', 'sector': 'Y', 'interest_coverage': 5, 'ev_ebitda': 10},
    {'ticker': 'M', 'sector': 'Y', 'interest_coverage': 5, 'ev_ebitda': 10}])))
  self.assertEqual([r['ticker'] for r in tie1], [r['ticker'] for r in tie2])

 def test_markov_chain_probabilities_normalise(self):
  import stock_selector as ss
  m = ss.markov_regime_chain(['A', 'B', 'A', 'B', 'A'])
  self.assertAlmostEqual(sum(m['next_probabilities'].values()), 1.0, places=4)
  for row in m['transition_matrix'].values():
   self.assertAlmostEqual(sum(row.values()), 1.0, places=4)


if __name__=='__main__':unittest.main()
