"""Persistent scheduling, per-publisher pacing, multi-source status, search and export."""
import concurrent.futures,contextlib,csv,datetime as dt,json,logging,os,signal,sqlite3,sys,threading,time
from pathlib import Path
from sources import F,M,G,UTC,guard
from reliability import gate,beat_info
from store import attest_latest
from store import db,writer,now,iso,when,recover,capture,next_due,host_for,feed_state,lens,events,read_cells,ready_row,LOG

def settings(home):
 path=Path(home)/'settings.json';s=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {};guard(isinstance(s,dict) and set(s)<= {'enabled','interval_minutes'},'unknown settings key');enabled=s.get('enabled',list(F));over=s.get('interval_minutes',{});guard(isinstance(enabled,list) and enabled and len(enabled)==len(set(enabled)) and all(k in F for k in enabled),'enabled must list known unique feeds');guard(isinstance(over,dict) and set(over)<=F.keys(),'interval_minutes: unknown feed');
 for k,n in over.items():guard(type(n) is int and F[k]['min_minutes']<=n<=1440,f'{k}: interval must be {F[k]["min_minutes"]}..1440 whole minutes')
 return {k:over.get(k,F[k]['minutes']) for k in enabled}
def record_policy(c,t,chosen):
 encoded=json.dumps(chosen,sort_keys=True);old=c.execute('select loaded,intervals from policy where single=1').fetchone()
 with c:
  if old and not c.execute('select 1 from policy_log limit 1').fetchone():c.execute('insert into policy_log values(?,?)',(old['loaded'],old['intervals']))
  if not old or json.loads(old['intervals'])!=chosen:c.execute('insert into policy_log values(?,?)',(t,encoded))
  c.execute('insert or replace into policy values(1,?,?)',(t,encoded))
def effective(c,home,t=None):
 if t is not None:
  row=c.execute('select intervals from policy_log where loaded<=? order by loaded desc limit 1',(iso(t),)).fetchone()
  return json.loads(row[0]) if row else {k:r['minutes'] for k,r in F.items()}  # No policy exists before the first run; no receipts there either.
 row=c.execute('select intervals from policy where single=1').fetchone();return json.loads(row[0]) if row else settings(home)
def pulse(c,boot,state,clock=now):
 with c:c.execute('insert into worker values(1,?,?,?,?) on conflict(single) do update set started=excluded.started,beat=excluded.beat,state=excluded.state,pid=excluded.pid',(boot,iso(clock()),state,os.getpid()))
def select_due(c,schedules,active,host_after,d,mono,remaining=None):
 busy={v[1] for v in active.values()};lst=[]
 for k in schedules:
  if k in active or remaining is not None and k not in remaining:continue
  h=host_for(k,d.date());due=next_due(c,k,d)
  if due<=iso(d) and h not in busy and host_after.get(h,0)<=mono:lst.append((due,k,h))
 picks=[]
 for _,k,h in sorted(lst):
  if len(picks)>=3-len(active):break
  if h not in busy:picks.append((k,h));busy.add(h)
 return picks

def sync_obsidian(c,home):
 """Keep the OBSIDIAN point-in-time store current. Opt-in: runs only after `data init` created it.
 A failure is logged and never interrupts collection."""
 from pathlib import Path as _P
 if not (_P(home)/'obsidian.sqlite3').is_file():return None
 try:
  from obsidian.data import service as DS
  r=DS.sync_journal(home,c)
  if r.get('snapshots'):LOG.info('OBSIDIAN store synced: %s',json.dumps(r))
  return r
 except Exception as e:
  LOG.warning('OBSIDIAN store not synced (%s); collection continues.',e);return None
def run(home,once=False,only=None,bind='127.0.0.1',port=8790,web=True,stop=None,checkpoint_path=None):
 schedules=settings(home)
 if only:guard(only in schedules,'feed not enabled');schedules={only:schedules[only]}
 stop=stop or threading.Event();active={};last_host={};res=[];srv=None
 from materialize import latest,checkpoint,restore_if_absent
 with writer(home),contextlib.ExitStack() as stack:
  if checkpoint_path:restore_if_absent(home,checkpoint_path)
  c=stack.enter_context(contextlib.closing(db(home,True)))
  recover(c,schedules);attest_latest(c);boot=iso(now());pulse(c,boot,'running');
  record_policy(c,boot,settings(home))
  if web and not once:
   from dashboard import start_server
   try:srv=start_server(home,bind,port);LOG.info('Read-only monitor listening on %s:%s',bind,port)
   except OSError as e:LOG.warning('Monitor could not bind (%s); collector continues without a dashboard',e)
  def poll(k):
   with contextlib.closing(db(home)) as cc:return capture(cc,k,schedules[k])
  remaining={k for k in schedules if next_due(c,k,now())<=iso(now())} if once else None;beat=0;export_at=0;checkpoint_at=0;dirty=False;export_dirty=False
  try:
   with concurrent.futures.ThreadPoolExecutor(max_workers=3,thread_name_prefix='PublicData') as pool:
    while not stop.is_set():
     mono=time.monotonic();d=now()
     if mono>=beat:pulse(c,boot,'running');beat=mono+10
     for k,(job,h) in list(active.items()):
      if not job.done():continue
      x=job.result();LOG.info('%s',json.dumps(x,ensure_ascii=False));
      if once:res.append(x)
      del active[k];last_host[h]=mono+2;dirty=True;export_dirty=True
     if (mono>=export_at or export_dirty and not active):
      try:latest(c,home);export_dirty=False;sync_obsidian(c,home)
      except (OSError,sqlite3.Error) as e:export_dirty=False;LOG.warning('Local CSV not refreshed (%s); live search still reads SQLite.',e)
      export_at=mono+15
     if checkpoint_path and dirty and mono>=checkpoint_at and not active and not select_due(c,schedules,active,last_host,d,mono,remaining):
      try:checkpoint(home,checkpoint_path);dirty=False
      except (ValueError,OSError,sqlite3.Error) as e:LOG.warning('%s',e)
      checkpoint_at=mono+300
     if once and not remaining and not active:break
     if once:
      for k in list(remaining):
       if next_due(c,k,d)>iso(d):remaining.remove(k)
     for k,h in select_due(c,schedules,active,last_host,d,mono,remaining):
      if remaining is not None:remaining.discard(k)
      active[k]=(pool.submit(poll,k),h)
     stop.wait(.75 if active or remaining else 2)
  finally:
   if srv:srv.shutdown();srv.server_close()
   pulse(c,boot,'stopped')
   try:
    latest(c,home)
    if checkpoint_path:checkpoint(home,checkpoint_path)
   except (ValueError,OSError,sqlite3.Error) as e:LOG.warning('Final export/checkpoint: %s',e)
 return res

def status(c,home,d=None):
 d=d or now();T=iso(d);enabled=effective(c,home);a=[];beat=c.execute('select * from worker where single=1').fetchone();active=beat_info(c,d)[0]
 for k,f in F.items():
  p,r,state=feed_state(c,k,d);due=next_due(c,k,d) if k in enabled else None;prior_issue=c.execute('select issue.kind,issue.reason,issue.at from issue join poll using(id) where feed=? and issue.at<=? order by id desc limit 1',(k,T)).fetchone();a.append({**f,'enabled':k in enabled,'interval_minutes':enabled.get(k),'state':state if k in enabled else 'disabled','last_started':p['started'] if p else None,'last_success':r['indexed'] if r else None,'last_kind':r['kind'] if r else None,'observations':r['n'] if r else 0,'last_issue':dict(prior_issue) if prior_issue else None,'next_check':due or ('on start' if k in enabled else None),'series':sum(m['feed']==k for m in M.values())})
 ok=sum(f['state']=='accepted' for f in a) if active else 0;count=c.execute('select count(*) from ready').fetchone()[0];ch=c.execute('select count(*) from event join ready using(id)').fetchone()[0];return dict(utc=T,worker_online=active,heartbeat=beat['beat'] if beat else None,scheduler_state=beat['state'] if beat else 'not started',registered_feeds=len(F),enabled_feeds=len(enabled),accepted_feeds=ok,enabled_series=sum(f['series'] for f in a if f['enabled']),accepted_polls=count,change_events=ch,feeds=a)
def catalogue(c,home,q='',geo='',fac='',feed='',d=None):
 hist=d is not None;d=d or now();online,heart=beat_info(c,d);q=q.casefold().split();g=geo.upper();f=fac.upper();ret=[];enabled=effective(c,home,d if hist else None)
 for i,m in M.items():
  if (g and g not in m['geos']) or (f and m['factor']!=f) or (feed and m['feed']!=feed):continue
  words=' '.join([i,m['label'],m['unit'],m['factor'],m['feed'],F[m['feed']]['publisher'],' '.join(G[a]+' '+a for a in m['geos'])]).casefold()
  syn={'inflation':'cpi hicp ppi','rates':'yield interest sofr','jobs':'unemployment labour','fx':'exchange rate currency','gdp':'growth output'}
  if not all(w in words or any(s in words for s in syn.get(w,'').split()) for w in q):continue
  a=lens(c,m['feed'],i,d);a['publisher']=F[m['feed']]['publisher']
  if m['feed'] not in enabled:a['state']='disabled';a['current_value']=None
  ret.append(gate(c,a,d,online,heart,hist))
 ret.sort(key=lambda x:(0 if q and any(w==x['id'].casefold() or w==x['factor'].casefold() for w in q) else 1, x['id']));return ret

def detail(c,home,i,at=None,period=None):
 guard(i in M,'unknown series');d=at or now();k=M[i]['feed'];x=lens(c,k,i,d,period);r=ready_row(c,k,iso(d));x['history']=[];x['changes']=[]
 if k not in effective(c,home,at):x['state']='disabled';x['current_value']=None
 if r:
  x['history']=[dict(z) for z in c.execute('select t as period,x as value,flag,pub as source_release from snap where feed=? and h=? and parser=? and series=? order by t desc limit 16',(k,r['h'],r['parser'],i))];x['changes']=[dict(z) for z in c.execute('select e.*,r.indexed as observed_utc from event e join ready r using(id) where e.series=? and r.indexed<=? order by r.indexed desc,e.t desc limit 16',(i,iso(d)))];
 online,heart=beat_info(c,d);return gate(c,x,d,online,heart,at is not None)

def export_rows(c,home):
 cols=['exported_utc','asof_utc','recheck_due_utc','usable_until_utc','worker_online','heartbeat','series','geos','atlas_factor','signal_group','publisher','measure','unit','period','current_value','screened_value','quality','source_flag','source_release','state','last_numeric_period','last_numeric_value','snapshot_seen_utc','snapshot_indexed_utc','contract_checked_utc','source_url','sha256'];yield cols
 for r in catalogue(c,home):
  old=r['last_numeric'] or {}; yield [r['asof_utc'],r['asof_utc'],r['due'],r['usable_until_utc'],r['worker_online'],r['heartbeat'],r['id'],'|'.join(r['geos']),r['factor'],r['signal_group'],r['publisher'],r['label'],r['unit'],r['period'],r['current_value'],r['screened_value'],r['quality'],r['source_flag'],r['source_release'],r['state'],old.get('period'),old.get('value'),r['snapshot_seen_utc'],r['snapshot_indexed_utc'],r['contract_checked_utc'],r['source_url'],r['snapshot_sha256']]
