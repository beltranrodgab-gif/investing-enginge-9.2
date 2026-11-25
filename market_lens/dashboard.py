"""Local, read-only monitor. No remote controls, arbitrary file paths or user-supplied network requests."""
import contextlib,csv,io,json,threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import urlsplit,parse_qs
from engine import status,catalogue,detail,export_rows
from plan_api import plan_view
from store import db,events,when
ROOT=Path(__file__).resolve().parent
class Server(ThreadingHTTPServer):daemon_threads=True;allow_reuse_address=True

def start_server(home,bind,port):
 class Get(BaseHTTPRequestHandler):
  server_version='MarketLens/6.8';sys_version=''
  def log_message(self,*_):pass
  def send(self,content,mime,code=200,filename=None):
   if not isinstance(content,bytes):content=content.encode('utf-8')
   self.send_response(code);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(content)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','no-referrer');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; form-action 'none'")
   if filename:self.send_header('Content-Disposition','attachment; filename="'+filename+'"')
   self.end_headers()
   if self.command!='HEAD':self.wfile.write(content)
  def handle_it(self):
   if len(self.path)>2048:return self.send('Request too long','text/plain',414)
   u=urlsplit(self.path);path=u.path;args=parse_qs(u.query,max_num_fields=8);arg=lambda k:args.get(k,[''])[0][:150]
   if path=='/':return self.send((ROOT/'monitor.html').read_bytes(),'text/html; charset=utf-8')
   if path=='/favicon.ico':return self.send(b'','image/x-icon',204)
   if path.startswith('/api/data/'):
    from obsidian.data.service import web_view
    try:v=web_view(home,path,arg)
    except FileNotFoundError:return self.send(json.dumps({'error':'no OBSIDIAN data store yet; run: market_lens.py data init'}),'application/json; charset=utf-8',404)
    except KeyError:return self.send('Not found','text/plain',404)
    return self.send(json.dumps(v,ensure_ascii=False,default=str),'application/json; charset=utf-8')
   with contextlib.closing(db(home)) as c:
    c.execute('pragma query_only=on');c.execute('BEGIN')
    if path=='/api/status':v=status(c,home)
    elif path=='/api/series':v=catalogue(c,home,arg('q'),arg('geo'),arg('factor'),arg('feed'))
    elif path=='/api/detail':v=detail(c,home,arg('id'),at=when(arg('asof')) if arg('asof') else None,period=arg('period') or None)
    elif path=='/api/changes':v=events(c,80)
    elif path in ('/api/plan','/api/jarvis'):v=plan_view(c,home,arg,path)
    elif path=='/latest.csv':
     s=io.StringIO();csv.writer(s,lineterminator='\n').writerows(export_rows(c,home));return self.send(s.getvalue(),'text/csv; charset=utf-8',filename='Market_Lens_Live_Latest.csv')
    else:return self.send('Not found','text/plain',404)
    return self.send(json.dumps(v,ensure_ascii=False),'application/json; charset=utf-8')
  def do_GET(self):
   try:self.handle_it()
   except (BrokenPipeError,ConnectionResetError):pass
   except (KeyError,ValueError):self.send('Invalid query','text/plain',400)
   except Exception:self.send('Monitor unavailable; inspect local worker logs.','text/plain',503)
  do_HEAD=do_GET
 srv=Server((bind,port),Get);threading.Thread(target=srv.serve_forever,daemon=True).start();return srv
