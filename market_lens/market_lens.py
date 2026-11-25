"""Market Lens Auto 6.8 — scheduled official macro releases. No accounts, keys or manual observations."""
import argparse,contextlib,csv,datetime as dt,json,logging,logging.handlers,os,signal,sqlite3,sys,threading
from pathlib import Path
from sources import F,M,G,guard
from store import db,now,when,events,writer
from engine import status,catalogue,detail,run,export_rows
ROOT=Path(__file__).resolve().parent
def logs(home):
 home.mkdir(parents=True,exist_ok=True);lg=logging.getLogger('marketlens');lg.setLevel(logging.INFO);fmt=logging.Formatter('%(asctime)sZ %(levelname)s %(message)s');import time;fmt.converter=time.gmtime
 h=logging.handlers.RotatingFileHandler(home/'worker.log',maxBytes=1048576,backupCount=3,encoding='utf-8');h.setFormatter(fmt);lg.addHandler(h)
 if sys.stdout is not None and sys.stdout.isatty():lg.addHandler(logging.StreamHandler())
def main():
 a=argparse.ArgumentParser(description=__doc__);a.add_argument('--data',type=Path,default=Path.home()/'MarketLens62',help='state folder (default ~/MarketLens62)');s=a.add_subparsers(dest='cmd',required=True)
 p=s.add_parser('run',help='check immediately when due, then automatically thereafter');p.add_argument('--bind',default='127.0.0.1');p.add_argument('--port',default=8790,type=int);p.add_argument('--no-dashboard',action='store_true');p.add_argument('--checkpoint',type=Path,help='optional bounded .ml35.xz workspace checkpoint; local installations normally use the durable SQLite file directly');p=s.add_parser('once',help='check due feeds once, respecting retry limits');p.add_argument('--feed',choices=F);s.add_parser('enable',help='install and start a per-user background worker on this computer');s.add_parser('disable',help='remove per-user auto-start; keep collected data');s.add_parser('status');s.add_parser('audit',help='verify stored raw bytes, timestamps and every accepted normalized cell');p=s.add_parser('read-csv',help='only fresh screened rows; stale or offline exports yield no live values');p.add_argument('file',type=Path);s.add_parser('changes');s.add_parser('catalog');p=s.add_parser('search');p.add_argument('words',nargs='*');p.add_argument('--geo',choices=G);p.add_argument('--factor');p=s.add_parser('lookup');p.add_argument('series');p.add_argument('--period');p.add_argument('--as-of',help='UTC or other timezone-qualified decision time');p=s.add_parser('export');p.add_argument('new_csv',type=Path);p=s.add_parser('backup');p.add_argument('new_database',type=Path)
 sp=s.add_parser('strategy',help='turn the collected journal into one explainable portfolio decision (no broker contact)')
 sp.add_argument('--vol',action='append',default=[],metavar='ASSET=VOL',help='annualised volatility, e.g. SPY=0.16 (repeatable)')
 sp.add_argument('--target-vol',default=0.10,type=float)
 sp.add_argument('--max-turnover',default=0.50,type=float)
 sp.add_argument('--history',type=Path,help='JSON list of previously recorded regimes, for hysteresis')
 sp.add_argument('--plan',action='store_true',help='also spread the account across the risk/horizon ladder')
 sp.add_argument('--account',default=0.0,type=float,help='account value for --plan, in your currency')
 sp.add_argument('--state',type=Path,help='file holding the previous decision, used to detect when a change is needed')
 sp.add_argument('--jarvis',action='store_true',help='embed the supervisor briefing and change triggers')
 jp=s.add_parser('jarvis',help='supervisor briefing: system health, change triggers, recommended book')
 jp.add_argument('--vol',action='append',default=[],metavar='ASSET=VOL',help='annualised volatility, e.g. SPY=0.16 (repeatable)')
 jp.add_argument('--target-vol',default=0.10,type=float)
 jp.add_argument('--max-turnover',default=0.50,type=float)
 jp.add_argument('--account',default=0.0,type=float,help='account value for the spread plan')
 jp.add_argument('--goal',default=1.25,type=float,help='target growth multiple for the plan, e.g. 1.25')
 jp.add_argument('--years',default=3.0,type=float,help='planning horizon in years')
 jp.add_argument('--max-dd',default=0.25,type=float,help='drawdown limit the plan must respect')
 jp.add_argument('--regime-scale',default=1.0,type=float,help='shrink every risk budget (0..1)')
 jp.add_argument('--state',type=Path,help='file holding the previous decision, used to detect when a change is needed')
 pp=s.add_parser('paper',help='mark a simulated account to market and rebalance it on paper (no broker contact)')
 pp.add_argument('--nav',default=100000.0,type=float,help='starting NAV when the paper account is first created')
 pp.add_argument('--vol',action='append',default=[],metavar='ASSET=VOL',help='annualised volatility, e.g. SPY=0.16 (repeatable)')
 pp.add_argument('--price',action='append',default=[],metavar='ASSET=P1,P2,...',help='recent daily prices per asset, oldest first (repeatable)')
 pp.add_argument('--price-file',type=Path,help='CSV of date,asset,price rows instead of --price')
 pp.add_argument('--cost-bps',default=10.0,type=float,help='round-trip cost charged on turnover, in basis points')
 pp.add_argument('--target-vol',default=0.10,type=float)
 pp.add_argument('--max-turnover',default=0.50,type=float)
 pp.add_argument('--account',type=Path,help='paper account file (default <state folder>/paper_account.json)')
 pp.add_argument('--reset',action='store_true',help='start a new account instead of continuing the stored one')
 pp.add_argument('--history',action='store_true',help='include the per-period NAV history in the output')
 pp.add_argument('--symbols',action='append',default=[],metavar='TICKER',help='fetch daily prices from Yahoo Finance (repeatable); omit --price/--price-file to use it')
 pp.add_argument('--range',default='2y',help='Yahoo history window for --symbols (default 2y)')
 pp.add_argument('--offline',action='store_true',help='use only cached prices; never contact Yahoo')
 pp.add_argument('--max-price-age',default=12.0,type=float,help='hours before a cached price is refetched')
 pp.add_argument('--auto-vol',action='store_true',help='estimate volatility from the fetched history instead of --vol')
 rp=s.add_parser('research',help='collect point-in-time market and filing research data into a separate store (never the official journal)')
 rp.add_argument('--init',action='store_true',help='create the research database (required the first time)')
 rp.add_argument('--symbols',action='append',default=[],metavar='TICKER',help='tickers to collect (repeatable)')
 rp.add_argument('--intervals',default='1d',help='comma-separated price intervals: 1d,1m,5m,15m,30m,60m,1wk (default 1d)')
 rp.add_argument('--range',default=None,help='price history window per interval (default: the provider limit)')
 rp.add_argument('--sec',action='store_true',help='also collect SEC point-in-time fundamentals and filing index')
 rp.add_argument('--short-interest',action='store_true',help='also collect short interest, current venue data with freshness verified (FINRA archive only as fallback)')
 rp.add_argument('--quotes',action='store_true',help='also collect bid/ask quotes with sizes (one exchange, not consolidated NBBO)')
 rp.add_argument('--options',action='store_true',help='also collect options chains and compute implied volatility, skew and put/call ratios')
 rp.add_argument('--consensus',action='store_true',help='also collect analyst consensus, ratings and institutional ownership (snapshot, no revision history)')
 rp.add_argument('--delisted',action='store_true',help='also index SEC archived filer quarters to make the issuer universe survivorship-aware')
 rp.add_argument('--delisted-quarters',default=2,type=int,help='quarters of SEC full-index to index for --delisted (default 2, max 40)')
 rp.add_argument('--delisting-events',action='store_true',help='with --delisted, also fetch Form 25 notices for real delisting dates and reasons (one request each)')
 rp.add_argument('--notice-limit',default=200,type=int,help='cap on Form 25 notices fetched per quarter (default 200)')
 rp.add_argument('--quote-max-age',default=None,type=float,help='minutes after which a quote is not treated as a live mark (default 30)')
 rp.add_argument('--no-universe',action='store_true',help='skip refreshing the SEC ticker universe')
 rp.add_argument('--asof',help='UTC-qualified time used to prove point-in-time reads')
 rp.add_argument('--coverage',action='store_true',help='print the coverage report for the stored data and exit')
 rp.add_argument('--journal',type=Path,help='official collection folder to check macro datasets against')
 rp.add_argument('--max-issuers',default=25,type=int,help='cap on issuers fetched from EDGAR in one run')
 rp.add_argument('--store',type=Path,help='research database path (default <state folder>/research.db)')
 g=a.parse_args();home=g.data.expanduser().resolve();j=lambda x:print(json.dumps(x,indent=2,ensure_ascii=False,default=str))
 if g.cmd in ('enable','disable'):
  import autostart;return autostart.main(g.cmd,home)
 if g.cmd=='read-csv':
  from reliability import read_export
  j(read_export(g.file));return 0
 if g.cmd=='catalog':j(list(M.values()));return 0
 if g.cmd=='once' or g.cmd=='run':
  logs(home);stop=threading.Event();signal.signal(signal.SIGINT,lambda *_:stop.set());signal.signal(signal.SIGTERM,lambda *_:stop.set());one=g.cmd=='once';r=run(home,one,getattr(g,'feed',None),getattr(g,'bind','127.0.0.1'),getattr(g,'port',8790),not getattr(g,'no_dashboard',False),stop,getattr(g,'checkpoint',None))
  if one:j(r);return int(any(e['state'] not in ('accepted','not due') for e in r))
  return 0
 # Validate strategy arguments *before* opening the journal, so a typo reports the typo rather than
 # "no live collection database yet".
 vols=None;hist=None
 if g.cmd=='paper':
  guard(g.nav>0,'--nav must be positive')
  guard(g.cost_bps>=0,'--cost-bps must be non-negative')
  guard(0<g.target_vol<=2,'--target-vol must be in (0, 2]')
  guard(0<g.max_turnover<=2,'--max-turnover must be in (0, 2]')
  prices={}
  for item in g.price:
   name,_,value=item.partition('=')
   guard(name and value,'--price expects ASSET=P1,P2,..., e.g. SPY=100,101,102')
   series=[]
   for piece in value.split(','):
    try: series.append(float(piece))
    except ValueError: raise ValueError('--price '+item+': not a number: '+piece) from None
   guard(len(series)>=2,'--price '+item+': need at least two prices to mark to market')
   guard(all(x>0 for x in series),'--price '+item+': prices must be positive')
   prices[name.upper()]=series
  if g.price_file:
   import csv as _csv
   for row in _csv.DictReader(g.price_file.open(encoding='utf-8-sig',newline='')):
    guard({'asset','price'}<=set(row),'--price-file needs asset,price columns')
    try: px=float(row['price'])
    except (TypeError,ValueError): raise ValueError('price-file: bad price for '+str(row.get('asset'))) from None
    guard(px>0,'price-file: prices must be positive')
    prices.setdefault(row['asset'].strip().upper(),[]).append(px)
  if not prices:
   guard(g.symbols,'paper needs --price ASSET=P1,P2,... , --price-file, or --symbols TICKER')
   guard(not g.auto_vol or len(g.symbols)>=1,'--auto-vol needs at least one --symbols ticker')
   guard(0<g.max_price_age<=24*30,'--max-price-age must be in (0, 720] hours')
 if g.cmd in ('strategy','jarvis','paper'):
  vols={}
  for item in g.vol:
   name,_,value=item.partition('=')
   guard(name and value,'--vol expects ASSET=VOL, e.g. SPY=0.16')
   try:v=float(value)
   except ValueError:raise ValueError('--vol '+item+': volatility is not a number') from None
   guard(0<v<5,'--vol '+item+': volatility must be between 0 and 5')
   vols[name.upper()]=v
  if g.cmd=='strategy':guard(vols,'strategy needs --vol for at least one asset, e.g. --vol SPY=0.16 --vol TLT=0.12')
  if g.cmd=='paper':
   guard(vols or g.auto_vol,'paper needs --vol for at least one asset (or --auto-vol with --symbols), e.g. --vol SPY=0.16')
  guard(0<g.target_vol<=2,'--target-vol must be in (0, 2]')
  guard(0<g.max_turnover<=2,'--max-turnover must be in (0, 2]')
  if g.cmd=='jarvis':
   guard(g.account>=0,'--account must be non-negative')
   guard(0<g.goal<=100,'--goal must be in (0, 100]')
   guard(0<g.years<=60,'--years must be in (0, 60]')
   guard(0<g.max_dd<1,'--max-dd must be in (0, 1)')
   guard(0<=g.regime_scale<=1,'--regime-scale must be in [0, 1]')
  if getattr(g,'history',None):
   hist=json.loads(Path(g.history).read_text('utf-8'));guard(isinstance(hist,list),'history must be a JSON list of regime names')
 if g.cmd=='research':
  import research as R, research_store as RS, market_data as MD
  spath=(g.store or (home/'research.db')).expanduser().resolve()
  intervals=[x.strip() for x in g.intervals.split(',') if x.strip()]
  guard(intervals,'--intervals needs at least one interval, e.g. --intervals 1d,1m')
  for iv in intervals:
   guard(iv in MD.INTERVAL_LIMITS,'unsupported interval '+iv+'; choose '+', '.join(sorted(MD.INTERVAL_LIMITS)))
  guard(0<g.max_issuers<=200,'--max-issuers must be in (1, 200]')
  asof=when(g.asof) if g.asof else None
  tickers=[]
  for t in g.symbols:
   x=(t or '').strip().upper()
   guard(x and all(ch.isalnum() or ch in '._-^=' for ch in x),'invalid ticker '+repr(t))
   if x not in tickers:tickers.append(x)
  rc=RS.connect(spath,create=g.init)
  try:
   if g.coverage:
    jc=None
    if g.journal:
     jc=db(g.journal.expanduser().resolve())
    try:
     j(R.render_text(R.coverage_report(rc,journal=jc)))
    finally:
     if jc is not None:jc.close()
    return 0
   guard(1<=g.delisted_quarters<=40,'--delisted-quarters must be in (1, 40]')
   guard(0<g.notice_limit<=2000,'--notice-limit must be in (1, 2000]')
   if g.quote_max_age is not None:guard(g.quote_max_age>0,'--quote-max-age must be positive')
   rep=R.run(rc,tickers,intervals=intervals,rng=g.range,with_sec=g.sec,with_short_interest=g.short_interest,
             with_universe=not g.no_universe,max_issuers=g.max_issuers,asof_utc=asof,
             with_quotes=g.quotes,with_options=g.options,with_consensus=g.consensus,
             with_delisted=g.delisted,delisted_quarters=g.delisted_quarters,
             with_delist_events=g.delisting_events,notice_limit=g.notice_limit,
             quote_freshness_minutes=g.quote_max_age)
  finally:
   rc.close()
  out={'store':str(spath),'summary':R.describe(rep)}
  for k in ('started_utc','finished_utc','symbols','intervals','failed','asof_utc'):
   if k in rep:out[k]=rep[k]
  if rep.get('market_data'):
   out['market_data']={k:v for k,v in rep['market_data'].items() if k!='ok'}
   out['market_data']['stored']=[{k:i.get(k) for k in ('symbol','interval','bars','new_bars','actions','window','history_limit','conflict_count')} for i in rep['market_data'].get('ok') or []]
  if rep.get('sec'):
   out['sec']={k:v for k,v in rep['sec'].items() if k!='ok'}
   out['sec']['issuers']=[{k:i.get(k) for k in ('symbol','cik','fundamentals','filings','filing_lag_days','no_xbrl_reason')} for i in rep['sec'].get('ok') or []]
  if rep.get('short_interest'):
   out['short_interest']={k:v for k,v in rep['short_interest'].items() if k!='borrow_rates_note'}
  if rep.get('quotes'):
   import quote_surface as _qs
   out['quotes']={'summary':_qs.describe_quotes(rep['quotes']),'stale':rep['quotes'].get('stale'),
                  'failed':rep['quotes'].get('failed'),'conflicts':rep['quotes'].get('conflicts',0),
                  'scope':'single venue (Nasdaq), not consolidated NBBO',
                  'stored':[{k:i.get(k) for k in ('symbol','bid','ask','last','age_minutes','current')} for i in rep['quotes'].get('ok') or []]}
  if rep.get('options'):
   out['options']={'failed':rep['options'].get('failed'),
                   'chains':[{k:i.get(k) for k in ('symbol','rows','spot','atm_call_iv','put_call_oi_ratio','surface_error')} for i in rep['options'].get('ok') or []],
                   'note':'implied volatility is computed here by inverting Black-Scholes on the mid; unpriced strikes are not extrapolated'}
  if rep.get('consensus'):
   import analyst_consensus as _ac
   out['consensus']={'summary':_ac.describe(rep['consensus']),'failed':rep['consensus'].get('failed'),
                     'revision_history_available':rep['consensus'].get('revision_history_available'),
                     'stored':[dict(i) for i in rep['consensus'].get('ok') or []]}
  if rep.get('delisted'):
   import delisted as _dl
   out['delisted']={'summary':_dl.describe(rep['delisted']),'quarters':rep['delisted'].get('quarters'),
                    'failed':rep['delisted'].get('failed'),
                    'delist_events':rep['delisted'].get('delist_events'),
                    'note':'event dates and merger cash prices are not a price series; delisted price history remains unavailable'}
  out['coverage']=rep['coverage']['totals']
  out['blocking_gaps']=[x['name'] for x in rep['coverage']['blocking_gaps']]
  j(out)
  if sys.stderr is not None:
   sys.stderr.write('\n'+R.render_text(rep['coverage'])+'\n')
  return 0
 with contextlib.closing(db(home)) as c:
  c.execute('pragma query_only=ON');c.execute('BEGIN')
  if g.cmd=='audit':
   from reliability import inspect_journal
   r=inspect_journal(c);j(r);return 0 if r['passed'] else 2
  if g.cmd=='status':j(status(c,home))
  elif g.cmd=='changes':j(events(c))
  elif g.cmd=='search':j([{k:r[k] for k in ('id','label','geos','factor','period','current_value','screened_value','quality','unit','state','usable_until_utc','snapshot_indexed_utc')} for r in catalogue(c,home,' '.join(g.words),g.geo or '',g.factor or '')])
  elif g.cmd=='lookup':j(detail(c,home,g.series,when(g.as_of) if g.as_of else None,g.period))
  elif g.cmd=='export':
   with g.new_csv.open('x',encoding='utf-8-sig',newline='') as f:csv.writer(f).writerows(export_rows(c,home))
   print(g.new_csv)
  elif g.cmd=='backup':
   # Reserve a new path exclusively; SQLite's backup API includes the committed WAL state.
   with g.new_database.open('xb'):pass
   with contextlib.closing(sqlite3.connect(g.new_database)) as new:c.backup(new);guard(new.execute('pragma integrity_check').fetchone()[0]=='ok','backup check failed')
   print(g.new_database)
  elif g.cmd=='paper':
   import paper_account as pa, paper_trader as pt
   apath=g.account or pa.default_path(home)
   if g.reset:
    state,created={'nav':g.nav,'weights':{},'cash':1.0,'periods':0,'history':[],'active_returns':[],'initial_nav':g.nav},True
   else:
    state,created=pa.open_account(apath,g.nav)
   # Total return is measured from the account's own opening balance, not from whatever --nav
   # happens to be on this call; otherwise a resumed account reports a meaningless number.
   initial=float(state.get('initial_nav') or (g.nav if created else state['nav']))
   price_report=None
   if g.symbols:
    import yahoo_prices as yp
    try:
     fetched,price_report=yp.fetch_prices(g.symbols,cache_path=apath.parent/yp.CACHE_NAME,max_age_hours=g.max_price_age,allow_network=not g.offline,rng=g.range)
    except yp.PriceError as e:
     raise RuntimeError('price fetch failed: '+str(e)) from None
    for sym,series in fetched.items():
     prices.setdefault(sym,series['prices'])
    if g.auto_vol:
     est,skipped=yp.realized_vols_from(fetched)
     for sym,v in est.items(): vols.setdefault(sym,v)
     if not vols: raise RuntimeError('could not estimate volatility from the fetched history: '+str(skipped))
     price_report['auto_vol']=est
     price_report['auto_vol_skipped']=skipped
    guard(prices,'no usable prices were returned for '+', '.join(g.symbols))
   from alpha_engine import run_alpha_step
   res=run_alpha_step(home/'latest_live.csv' if (home/'latest_live.csv').is_file() else home/'latest.csv',vols,at=None,prev_weights=state['weights'],target_vol=g.target_vol,max_turnover=g.max_turnover)
   failed_closed=not res.get('weights')
   step=pt.step_paper_book(res.get('weights') or {},prices,state['weights'],nav=state['nav'],cost_bps=g.cost_bps,max_turnover=g.max_turnover,regime=res.get('regime'),emergency=failed_closed)
   prev_nav=state['nav']
   state={'nav':step['nav_after_cost'],'weights':step['weights'],'cash':step['cash'],'periods':state['periods']+1,'initial_nav':initial,'started_utc':state.get('started_utc') or now(),'last_utc':now(),'history':(state.get('history') or [])+[{'utc':now(),'nav':step['nav_after_cost'],'period':state['periods']+1}],'active_returns':(state.get('active_returns') or [])+[step['nav_after_cost']/prev_nav-1.0]}
   if not pa.save(apath,state): raise RuntimeError('could not save the paper account to '+str(apath))
   out=dict(step)
   if failed_closed: out['reason']=res.get('reason') or 'no usable signal'
   out['nav']=round(state['nav'],4)
   out['period']=state['periods']
   out['account']=str(apath)
   if price_report:
    out['prices']=price_report
    out['price_source']=yp.describe(price_report)
   out['created']=created
   out['starting_nav']=round(initial,2)
   out['total_return']=round(state['nav']/initial-1.0,6) if initial else None
   out['risk_scale_suggestion']=pt.adapt_risk_from_paper(state.get('active_returns') or [])
   if g.history: out['history']=state.get('history') or []
   j(out)
  elif g.cmd in ('strategy','jarvis'):
   from strategy_engine import decide
   from jarvis_supervisor import assess_change,supervisor_view,load_previous_state,save_state,recommended_book
   decision=decide(c,home,vols or {},run_history=hist,target_vol=g.target_vol,max_turnover=g.max_turnover)
   prev=load_previous_state(getattr(g,'state',None))
   if g.cmd=='strategy':
    out=decision
    if getattr(g,'jarvis',False) or getattr(g,'plan',False):
     plan=None
     if getattr(g,'plan',False):
      from portfolio_optimizer import build_plan
      plan=build_plan(account_value=g.account,asset_vols=vols or None,regime_scale=decision.get('risk_scale',1.0),regime=decision.get('regime','uncertain'))
     out=dict(decision);out['supervisor']=supervisor_view(status(c,home),decision,plan,prev)
     if plan:out['plan']=plan['plan'];out['operations']=plan['operations'];out['frontier']=plan['frontier']
    j(out)
   else:
    from portfolio_optimizer import build_plan
    plan=build_plan(account_value=g.account,asset_vols=vols or None,regime_scale=g.regime_scale if g.regime_scale!=1.0 else decision.get('risk_scale',1.0),goal_multiple=g.goal,years=g.years,max_drawdown=g.max_dd,regime=decision.get('regime','uncertain'))
    view=supervisor_view(status(c,home),decision,plan,prev,max_drawdown=g.max_dd)
    # The regime was only nested under ``decision``, which made the top-level view inconsistent
    # with ``/api/jarvis``, where it is a first-class field.
    view['regime']=decision.get('regime')
    # Report the scale the plan actually used: an explicit --regime-scale wins over the regime's.
    view['risk_scale']=(g.regime_scale if g.regime_scale!=1.0 else decision.get('risk_scale',1.0))
    view['decision']={k:decision.get(k) for k in ('asof_utc','regime','regime_confirmed','risk_scale','quadrant','target_weights','booked_weights','cash','portfolio_vol_estimate','target_vol','inputs_used')}
    view['plan']=plan['plan'];view['operations']=plan['operations'];view['frontier']={'min_variance':plan['frontier'].get('min_variance'),'tangency':plan['frontier'].get('tangency'),'points':plan['frontier'].get('points'),'cash':plan['frontier'].get('cash')}
    j(view)
   save_state(getattr(g,'state',None),decision)
 return 0
if __name__=='__main__':
 try:sys.exit(main())
 except (ValueError,OSError,RuntimeError,sqlite3.Error) as e:
  if sys.stderr:print('Market Lens:',str(e),file=sys.stderr)
  if logging.getLogger('marketlens').hasHandlers():logging.getLogger('marketlens').error('%s',e)
  sys.exit(1)
