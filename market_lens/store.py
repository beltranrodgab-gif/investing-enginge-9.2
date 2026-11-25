"""Crash-safe SQLite journal. Accepted snapshots only; local receipt, publication hint and reference period are separate."""
import collections,contextlib,datetime as dt,hashlib,json,logging,os,sqlite3,time,zlib
from pathlib import Path
from sources import F,M,VERS,UTC,guard,parse,request,end,num
from net import fetch,FetchFailure
NAME='macro-live.sqlite3'; APP=350102; LOG=logging.getLogger('marketlens')
def now():return dt.datetime.now(UTC)
def iso(d):
 guard(isinstance(d,dt.datetime) and d.tzinfo is not None,'timezone-aware time required');return d.astimezone(UTC).isoformat(timespec='microseconds').replace('+00:00','Z')
def when(s):
 d=dt.datetime.fromisoformat(s.replace('Z','+00:00'));guard(d.tzinfo is not None,'include a timezone (e.g. Z)');return d.astimezone(UTC)
def later(d,sec):
 limit=dt.datetime.max.replace(tzinfo=UTC);return iso(limit) if sec>=(limit-d).total_seconds()-1 else iso(d+dt.timedelta(seconds=sec))
def H(b):return hashlib.sha256(b).hexdigest()
def db(home,init=False):
 home=Path(home);p=home/NAME
 if init:home.mkdir(parents=True,exist_ok=True)
 else:guard(p.is_file(),'no live collection database yet')
 c=sqlite3.connect(p,timeout=15);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');aid=c.execute('pragma application_id').fetchone()[0];tables=c.execute("select count(*) from sqlite_master where type='table'").fetchone()[0]
 if tables and aid!=APP:c.close();raise ValueError('not a 6.8 collector database; use a fresh data directory')
 if init:
  c.execute('PRAGMA journal_mode=WAL');c.execute(f'pragma application_id={APP}');c.executescript('''
  CREATE TABLE IF NOT EXISTS poll(id INTEGER PRIMARY KEY,feed TEXT NOT NULL,started TEXT NOT NULL,request_url TEXT NOT NULL,request_body TEXT NOT NULL);
  CREATE INDEX IF NOT EXISTS poll_feed ON poll(feed,started,id);
  CREATE TABLE IF NOT EXISTS object(h TEXT PRIMARY KEY, bytes INTEGER NOT NULL, zipped BLOB NOT NULL);
  CREATE TABLE IF NOT EXISTS got(id INTEGER PRIMARY KEY REFERENCES poll,seen TEXT NOT NULL,h TEXT NOT NULL REFERENCES object,final_url TEXT NOT NULL,etag TEXT,lmod TEXT);
  CREATE TABLE IF NOT EXISTS snap(feed TEXT NOT NULL,h TEXT NOT NULL REFERENCES object,parser TEXT NOT NULL,series TEXT NOT NULL,t TEXT NOT NULL,x TEXT,flag TEXT NOT NULL,pub TEXT NOT NULL,pos TEXT NOT NULL,PRIMARY KEY(feed,h,parser,series,t)) WITHOUT ROWID;
  CREATE TABLE IF NOT EXISTS ready(id INTEGER PRIMARY KEY REFERENCES got,checked TEXT NOT NULL,indexed TEXT NOT NULL,hint TEXT NOT NULL,parser TEXT NOT NULL,n INTEGER NOT NULL,kind TEXT NOT NULL,added INTEGER NOT NULL,revised INTEGER NOT NULL,other INTEGER NOT NULL,due TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY REFERENCES poll,at TEXT NOT NULL,kind TEXT NOT NULL,reason TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS event(id INTEGER REFERENCES poll,series TEXT,t TEXT,kind TEXT,old_x TEXT,new_x TEXT,old_flag TEXT,new_flag TEXT,old_pub TEXT,new_pub TEXT,PRIMARY KEY(id,series,t)) WITHOUT ROWID;
  CREATE INDEX IF NOT EXISTS event_series ON event(series,id);
  CREATE TABLE IF NOT EXISTS attestation(id INTEGER REFERENCES ready,version TEXT NOT NULL,at TEXT NOT NULL,ok INTEGER NOT NULL,detail TEXT NOT NULL,PRIMARY KEY(id,version));
  CREATE TABLE IF NOT EXISTS policy_log(loaded TEXT PRIMARY KEY,intervals TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS policy(single INTEGER PRIMARY KEY CHECK(single=1),loaded TEXT,intervals TEXT);
  CREATE TABLE IF NOT EXISTS plan(feed TEXT PRIMARY KEY,due TEXT NOT NULL,strikes INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS pause(host TEXT PRIMARY KEY,until TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS worker(single INTEGER PRIMARY KEY CHECK(single=1),started TEXT,beat TEXT,state TEXT,pid INTEGER);
  ''');
  for tab in ('poll','object','got','snap','ready','issue','event','policy_log','attestation'):
   for action in ('update','delete'):c.execute(f"CREATE TRIGGER IF NOT EXISTS forbid_{tab}_{action} BEFORE {action} ON {tab} BEGIN SELECT RAISE(ABORT,'append-only observation ledger'); END")
  c.commit()
 c.execute('pragma synchronous=FULL');return c
@contextlib.contextmanager
def writer(home):
 p=Path(home);p.mkdir(parents=True,exist_ok=True);fd=open(p/'worker.lock','a+b');fd.seek(0,2)
 if fd.tell()==0:fd.write(b'0');fd.flush()
 fd.seek(0)
 try:
  if os.name=='nt':import msvcrt;msvcrt.locking(fd.fileno(),msvcrt.LK_NBLCK,1)
  else:import fcntl;fcntl.flock(fd.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
 except OSError:fd.close();raise RuntimeError('This data directory already has a worker. Use status or the dashboard.') from None
 try:yield
 finally:fd.close()

def setplan(c,k,t,strikes):c.execute('insert into plan values(?,?,?) on conflict(feed) do update set due=excluded.due,strikes=excluded.strikes',(k,t,strikes))
def recover(c,intervals,clock=now):
 """Only under exclusive worker lock. A crashed candidate never becomes accepted on restart."""
 d=clock();rs=c.execute('select p.id,p.feed,p.started from poll p left join ready r using(id) left join issue f using(id) where r.id is null and f.id is null').fetchall()
 with c:
  for r in rs:
   k=r['feed'];n=iso(max(d,when(r['started'])));c.execute('insert into issue values(?,?,?,?)',(r['id'],n,'interrupted','Worker stopped before searchable acceptance; previous snapshot is last-known only.'));setplan(c,k,max(n,later(when(r['started']),F[k]['min_minutes']*60)) if k=='us-bls' else n,1)
 return len(rs)

def planning(c,k):return c.execute('select due,strikes from plan where feed=?',(k,)).fetchone()
def host_for(k,d):
 from urllib.parse import urlsplit
 return urlsplit(request(k,d)[0]).hostname
def next_due(c,k,d):
 p=planning(c,k);pause=c.execute('select until from pause where host=?',(host_for(k,d.date()),)).fetchone();return max(p['due'] if p else '',pause['until'] if pause else '')
def ready_row(c,k,T):
 return c.execute('''select p.id,p.started,r.*,g.h,g.seen,g.final_url,g.lmod from ready r join poll p using(id) join got g using(id) where p.feed=? and r.indexed<=? order by r.indexed desc,p.id desc limit 1''',(k,T)).fetchone()
def read_cells(c,k,h,parser):return {(s,t):(x,fl,pub,pos) for s,t,x,fl,pub,pos in c.execute('select series,t,x,flag,pub,pos from snap where feed=? and h=? and parser=?',(k,h,parser))}
def replay_prior(c,k,r):
 obj=c.execute('select bytes,zipped from object where h=?',(r['h'],)).fetchone();b=zlib.decompress(obj['zipped']);guard(len(b)==obj['bytes'] and H(b)==r['h'],'wrong committed raw object');return parse(k,b,when(r['seen']),when(r['started']).date())
def attest_latest(c,clock=now):
 # No HTTP, no backdated receipt. Upgrade records a NEW check of the current accepted snapshot only.
 for k in F:
  t=clock();r=ready_row(c,k,iso(t))
  if not r or c.execute('select 1 from attestation where id=? and version=?',(r['id'],VERS)).fetchone():continue
  try:
   o=replay_prior(c,k,r);guard(o.cells==read_cells(c,k,r['h'],r['parser']),'normalized rows differ from publisher replay');ok,note=1,'full object + all normalized cells match'
  except Exception as ex:ok,note=0,str(ex)[:220]
  with c:c.execute('insert into attestation values(?,?,?,?,?)',(r['id'],VERS,iso(clock()),ok,note))
def compare(o,old,has_prior):
 if not has_prior:return []
 oldtail={};newtail={}
 for i,t in old:oldtail[i]=max(t,oldtail.get(i,''))
 for i,t in o.cells:newtail[i]=max(t,newtail.get(i,''))
 guard(oldtail.keys()==newtail.keys() and all(newtail[i]>=t for i,t in oldtail.items()),'series disappeared or tail moved backwards')
 for i,t in old:guard((i,t) in o.cells or t<o.window[i],'previously present period silently removed: '+i+' '+t)
 events=[]
 for (i,t),(x,fl,pub,pos) in sorted(o.cells.items()):
  prior=old.get((i,t));a,af,ap=prior[:3] if prior else (None,'','')
  if not prior:kind='missing notice' if x is None else 'new period' if t>oldtail[i] else 'backfill'
  elif ap and pub and when(pub)<when(ap):raise ValueError('publisher cell edition regressed: '+i+' '+t)
  elif a!=x:kind='withdrawn' if x is None else 'now available' if a is None else 'revision'
  elif af!=fl:kind='quality change'
  elif ap!=pub:kind='edition change'
  else:continue
  events.append((i,t,kind,a,x,af,fl,ap,pub))
 return events

def capture(c,k,minutes,clock=now,fetcher=fetch,force=False):
 start=clock();beg=iso(start);url,post=request(k,start.date());due=next_due(c,k,start)
 if not force and due>beg:return dict(feed=k,state='not due',next_check=due)
 last=c.execute('select max(stamp) from (select max(started) as stamp from poll where feed=? union all select max(indexed) from ready join poll using(id) where feed=? union all select max(at) from issue join poll using(id) where feed=?)',(k,k,k)).fetchone()[0];guard(not last or beg>=last,'wall clock regressed; refusing to backdate a poll');
 with c:i=c.execute('insert into poll(feed,started,request_url,request_body) values(?,?,?,?)',(k,beg,url,(post or b'').decode())).lastrowid
 seen='';has_body=False
 try:
  b,final,etag,lmod=fetcher(url,post);dt_seen=clock();seen=iso(dt_seen);guard(seen>=beg,'clock regressed during retrieval');h=H(b)
  with c:
   if not c.execute('select 1 from object where h=?',(h,)).fetchone():c.execute('insert into object values(?,?,?)',(h,len(b),zlib.compress(b,6)))
   c.execute('insert into got values(?,?,?,?,?,?)',(i,seen,h,final,etag,lmod))
  has_body=True;o=parse(k,b,dt_seen,start.date());last=ready_row(c,k,beg);comparable=False;old={}
  if last:
   if last['hint'] and o.hint:guard(when(o.hint)>=when(last['hint']),'publisher edition regressed; not a new revision')
   if last['parser']==VERS:old=read_cells(c,k,last['h'],last['parser']);comparable=True
   else:
    try:old=replay_prior(c,k,last).cells;comparable=True
    except (ValueError,KeyError,TypeError,ArithmeticError):pass
  events=compare(o,old,comparable);a=sum(e[2]=='new period' for e in events);r=sum(e[2] in ('revision','withdrawn','now available') for e in events);other=len(events)-a-r;kind='baseline' if not last else 'parser baseline' if not comparable else 'changed' if events else 'unchanged';
  with c:
   if not c.execute('select 1 from snap where feed=? and h=? and parser=? limit 1',(k,h,VERS)).fetchone():c.executemany('insert into snap values(?,?,?,?,?,?,?,?,?)',((k,h,VERS,s,t,x,f,p,at) for (s,t),(x,f,p,at) in o.cells.items()))
   c.executemany('insert into event values(?,?,?,?,?,?,?,?,?,?)',((i,*e) for e in events))
  checked=iso(clock());guard(checked>=seen,'clock regressed during validation');stored=c.execute('select zipped,bytes from object where h=?',(h,)).fetchone();guard(stored['bytes']==len(b) and H(zlib.decompress(stored['zipped']))==h,'failed local committed-body readback');guard(o.cells==read_cells(c,k,h,VERS),'committed values/flags/locators differ from received snapshot');indexed=clock();at=iso(indexed);guard(at>=checked,'clock regressed before local index receipt');due=later(indexed,minutes*60)
  with c:c.execute('insert into ready values(?,?,?,?,?,?,?,?,?,?,?)',(i,checked,at,o.hint,VERS,len(o.cells),kind,a,r,other,due));setplan(c,k,due,0);c.execute('insert into attestation values(?,?,?,?,?)',(i,VERS,at,1,'fetch + exact committed-cell replay'))
  return dict(feed=k,poll=i,state='accepted',kind=kind,indexed_utc=at,observations=len(o.cells),new_periods=a,revisions=r,other_changes=other,next_check=due)
 except Exception as e:
  c.rollback();d=max(clock(),when(beg),when(seen or beg));n=(planning(c,k)['strikes'] if planning(c,k) else 0)+1;retry=getattr(e,'retry',0);wait=max(300*2**min(n-1,6),retry,21600 if k=='us-bls' else 0);due=later(d,wait);tag='quarantined' if has_body else 'failed';reason=str(e)[:350] or type(e).__name__
  with c:
   c.execute('insert into issue values(?,?,?,?)',(i,iso(d),tag,reason));setplan(c,k,due,n)
   if isinstance(e,FetchFailure) and (retry or 'HTTP 429' in reason):c.execute('insert into pause values(?,?) on conflict(host) do update set until=max(pause.until,excluded.until)',(host_for(k,start.date()),later(d,max(300,retry))))
  return dict(feed=k,poll=i,state=tag,reason=reason,next_check=due)

def poll_asof(c,k,T):
 return c.execute('''select p.id,p.started,r.indexed,f.at,f.kind,f.reason from poll p left join ready r using(id) left join issue f using(id) where p.feed=? and p.started<=? order by p.id desc limit 1''',(k,T)).fetchone()
def feed_state(c,k,d):
 T=iso(d);p=poll_asof(c,k,T);last=ready_row(c,k,T);pstate='not checked' if not p else p['kind'] if p['at'] and p['at']<=T else ('accepted' if p['indexed'] and p['indexed']<=T else 'checking');state=pstate if pstate!='accepted' else 'overdue' if T>later(when(last['due']),300) else 'accepted';return p,last,state

def lens(c,k,i,d,specific=None):
 T=iso(d);p,last,state=feed_state(c,k,d);latest=known=None;st=state;member=False
 if last:
  args=k,last['h'],last['parser'],i;sel='select t as period,x as value,flag,pub as source_release,pos as locator from snap where feed=? and h=? and parser=? and series=?';tail=c.execute(sel+' order by t desc limit 1',args).fetchone();known=c.execute(sel+' and x is not null order by t desc limit 1',args).fetchone();latest=tail if specific is None else c.execute(sel+' and t=?',args+(specific,)).fetchone();member=latest is not None
  if st=='accepted':
   if not tail or tail['value'] is None:st='latest value missing'
   elif (d.date()-end(tail['period'],M[i]['freq'])).days>M[i]['tail_days']:st='reference old'
   elif not member or latest['value'] is None:st='period not available'
  if specific and not member:
   archived=c.execute('''select z.t as period,z.x as value,z.flag,z.pub as source_release,z.pos as locator,r.indexed,g.h from ready r join poll p using(id) join got g using(id) join snap z on z.feed=p.feed and z.h=g.h and z.parser=r.parser where p.feed=? and z.series=? and z.t=? and r.indexed<=? order by r.indexed desc limit 1''',(k,i,specific,T)).fetchone();latest=archived if archived else None
 return {**M[i], 'asof_utc':T,'state':st,'current_value':latest['value'] if st=='accepted' and member else None,'period':latest['period'] if latest else '', 'record':dict(latest) if latest else None,'last_numeric':dict(known) if known else None,'snapshot_seen_utc':last['seen'] if last else None,'snapshot_indexed_utc':last['indexed'] if last else None,'snapshot_sha256':last['h'] if last else None,'source_url':last['final_url'] if last else None,'edition_hint':last['hint'] if last else '', 'in_snapshot':member,'last_poll_id':p['id'] if p else None,'accepted_id':last['id'] if last else None,'snapshot_parser':last['parser'] if last else None,'due':last['due'] if last else None}

def events(c,limit=100,i=None,feed=None):
 q='''select e.*,r.indexed as observed_utc,p.feed,g.h as sha256 from event e join ready r using(id) join poll p using(id) join got g using(id)''';conds=[];args=[]
 if i:conds.append('e.series=?');args.append(i)
 if feed:conds.append('p.feed=?');args.append(feed)
 return [dict(r) for r in c.execute(q+(' where '+' and '.join(conds) if conds else '')+' order by r.indexed desc,r.id desc,e.t desc,e.series limit ?',args+[limit])]
