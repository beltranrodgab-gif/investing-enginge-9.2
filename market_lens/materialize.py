"""Atomically publish a search/export file, optionally keep bounded restart checkpoints for the workspace preview."""
import contextlib,csv,lzma,os,shutil,sqlite3,tempfile,time
from pathlib import Path
from store import db,NAME,APP,now,iso,when,guard

def latest(c,home):
 from engine import export_rows
 home=Path(home);path=home/'latest_live.csv';tmp=None
 try:
  with tempfile.NamedTemporaryFile('w',dir=home,prefix='latest-',suffix='.tmp',encoding='utf-8-sig',newline='',delete=False) as f:
   tmp=Path(f.name);w=csv.writer(f,lineterminator='\n');c.execute('BEGIN')
   try:w.writerows(export_rows(c,home))
   finally:c.rollback()
  os.replace(tmp,path)
 finally:
  if tmp:tmp.unlink(missing_ok=True)

def checkdb(p):
 with contextlib.closing(sqlite3.connect(p)) as c:
  guard(c.execute('pragma application_id').fetchone()[0]==APP and c.execute('pragma quick_check').fetchone()[0]=='ok','wrong/damaged checkpoint database')

def checkpoint(home,path,limit=850*1024):
 path=Path(path);guard(path.name.endswith('.ml35.xz'),'checkpoint name must end in .ml35.xz');path.parent.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(dir=home) as td, tempfile.TemporaryDirectory(dir=path.parent) as td2:
  plain=Path(td)/'checkpoint.sqlite';tmp=Path(td2)/'checkpoint.xz';
  with contextlib.closing(db(home)) as c,contextlib.closing(sqlite3.connect(plain)) as out:c.backup(out)
  checkdb(plain)
  with plain.open('rb') as f,lzma.open(tmp,'wb',preset=3) as out:
   while b:=f.read(1<<18):out.write(b)
  guard(tmp.stat().st_size<=limit,'preview checkpoint exceeds size budget; keeping previous checkpoint. Live database is untouched.');os.replace(tmp,path)

def restore_if_absent(home,path):
 path=Path(path);home=Path(home);p=home/NAME
 if p.exists() or not path.exists():return False
 guard(path.name.endswith('.ml35.xz'),'wrong checkpoint extension');home.mkdir(parents=True,exist_ok=True)
 with tempfile.TemporaryDirectory(dir=home) as td:
  tmp=Path(td)/'restore.sqlite'
  with lzma.open(path,'rb') as f,tmp.open('wb') as out:
   while b:=f.read(1<<18):out.write(b);guard(out.tell()<=256*1024**2,'oversized preview checkpoint')
  checkdb(tmp);os.replace(tmp,p)
 return True
