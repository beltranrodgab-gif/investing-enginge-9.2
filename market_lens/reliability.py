"""Operational, source-quality and clock gates. Observational values are not executable prices / return forecasts."""
import collections,contextlib,csv,datetime as dt,hashlib,json,sqlite3,zlib
from pathlib import Path
from sources import M,F,VERS,UTC,guard,end
from store import now,when,iso,later,db,ready_row,read_cells,replay_prior,H
TTL=45
# A fallback may only stand in for a series measured in the *same unit*. CPI level (index) is not a
# year-on-year percent, and the euro-area 3M rate is not the UK 3M rate, so those two entries were
# removed: silently substituting them fed an index level into a percent signal and a euro rate into
# a sterling one. Only same-unit alternatives remain.
FALLBACKS={}
IMMUTABLE_KEYS=('period','snapshot_seen_utc','snapshot_indexed_utc','snapshot_sha256','source_url',
                'edition_hint','accepted_id','snapshot_parser','due','in_snapshot','last_poll_id')

def beat_info(c,d):
 r=c.execute('select * from worker where single=1').fetchone();return (bool(r and r['state']=='running' and 0<=(d-when(r['beat'])).total_seconds()<TTL),r['beat'] if r else None)

def quality(m,rec):
 value=(rec or {}).get('value');flag=(rec or {}).get('flag','');k=m['feed'];plain=flag.strip();lower=plain.lower();tokens=set(lower.split('|'))|set(lower.split())
 # An explicit normal status is normal on every publisher. Eurostat used to fall through to
 # "publisher flag: A" and silently lose the value while a blank flag was accepted.
 if value is None:return 'missing'
 if plain in ('','A'):return 'unflagged / normal'
 if k.startswith('ca-') and plain in 'BCDE' and len(plain)==1:return 'publisher quality grade '+plain
 if 'p' in tokens or plain=='P':return 'provisional'
 if 'e' in tokens or plain=='E':return 'estimated'
 if 'b' in lower.split('|') or 'b' in tokens:return 'series break'
 return 'publisher flag: '+plain[:95]

def group(m):
 i=m['id'];f=m['factor'];geo=m['geos'][0];return ('shared:EURUSD' if i=='ECB.USD' else 'shared:ECB-policy' if i=='bis-rates.XM' else 'shared:EUR-forward' if i=='FIN.EA20.fx3m' else geo+':'+f if f else 'input:'+i)

def gate(c,row,d,online,heartbeat,historical=False):
 checked=None;rec={k:v for k,v in (row.get('record') or {}).items() if k not in IMMUTABLE_KEYS};q=quality(row,rec);state=row['state'];rid=row.get('accepted_id');recorded=rec.get('value')
 try:
  r=c.execute('select at,ok from attestation where id=? and version=? and at<=?',(rid,VERS,iso(d))).fetchone() if rid else None
 except sqlite3.OperationalError:r=None
 if r and r['ok']:checked=r['at']
 if state=='accepted' and not checked:state='not revalidated at this time'
 if not historical and not online and state=='accepted':state='worker offline'
 cv=row['current_value'] if state=='accepted' else None;eligible=(cv is not None and q=='unflagged / normal');until=None
 if cv is not None:
  # A materialized row cannot silently remain healthy after a crash or after its observation tail expires.
  expiry=[later(when(row['due']),300)];tail=c.execute('select max(t) from snap where feed=? and h=? and parser=? and series=?',(row['feed'],row['snapshot_sha256'],row['snapshot_parser'],row['id'])).fetchone()[0]
  # No stored rows for this series yet: the observation tail is unknown, so fail closed on the
  # receipt/heartbeat windows only instead of raising (this used to take down /api/series).
  if tail is not None:
   expiry.append(iso(dt.datetime.combine(end(tail,row['freq'])+dt.timedelta(days=row['tail_days']+1),dt.time(),tzinfo=UTC)))
  if not historical and heartbeat is not None:
   expiry.append(later(when(heartbeat),TTL))
  until=min(expiry)
 fb_id=FALLBACKS.get(row['id']) if not eligible else None
 row.update(state=state,current_value=cv,screened_value=cv if eligible else None,quality=q,fallback_series=fb_id,source_flag=rec.get('flag',''),source_release=rec.get('source_release') or row.get('edition_hint'),worker_online=None if historical else online,heartbeat=None if historical else heartbeat,usable_until_utc=until,contract_checked_utc=checked,signal_group=group(row),mode='historical collector receipt, not strategy acknowledgement' if historical else 'live');return row

def read_export(path,at=None):
 t=at or now();out=[];p=Path(path)
 if not p.is_file():return out  # absent export == no fresh signals; callers already fail closed
 with p.open(encoding='utf-8-sig',newline='') as f:
  r=csv.DictReader(f);guard({'series','asof_utc','usable_until_utc','state','worker_online','screened_value','contract_checked_utc','snapshot_indexed_utc','quality','sha256'}<=set(r.fieldnames or []),'not a 3.6 screened export')
  for x in r:
   try:
    if x['state']=='accepted' and x['worker_online']=='True' and x['screened_value']!='' and x['quality']=='unflagged / normal' and x['usable_until_utc'] and when(x['snapshot_indexed_utc'])<=when(x['asof_utc']) and when(x['contract_checked_utc'])<=when(x['asof_utc'])<=t<when(x['usable_until_utc']):out.append(x)
   except (ValueError,TypeError):
    continue  # a malformed row is skipped, never promoted
 return out

def inspect_journal(c):
 guard(c.execute('pragma integrity_check').fetchone()[0]=='ok' and not c.execute('pragma foreign_key_check').fetchall(),'SQLite integrity failed');issues=[];n=0;cells=0;legacy=0;hashes=set();rows=c.execute('select p.feed,p.started,r.*,g.h,g.seen from ready r join poll p using(id) join got g using(id) order by r.id');
 for r in rows:
  try:
   guard(r['started']<=r['seen']<=r['checked']<=r['indexed']<r['due'],'receipt chronology');o=replay_prior(c,r['feed'],r);stored=read_cells(c,r['feed'],r['h'],r['parser']);guard(o.cells==stored and r['n']==len(stored),'contract/raw-cell mismatch');cells+=len(stored);hashes.add(r['h']);n+=1;legacy+=int(r['parser']!=VERS)
  except Exception as ex:issues.append({'id':r['id'],'feed':r['feed'],'issue':str(ex)[:250]})
 for row in c.execute('select * from object'):
  try:b=zlib.decompress(row['zipped']);guard(len(b)==row['bytes'] and H(b)==row['h'],'bad object')
  except Exception as ex:issues.append(dict(object=row['h'],issue=str(ex)))
 guard(not c.execute('select count(*) from issue join ready using(id)').fetchone()[0],'poll both failed and accepted');return dict(replayed_accepted_polls=n,accepted_distinct_bodies=len(hashes),compared_cells_across_polls=cells,inherited_polls=n and legacy or 0,issues=issues,passed=not issues)
