"""Bounded, TLS-verified public GET/POST requests; no keys, no ambient auth or user-supplied URLs."""
import datetime as dt,email.utils,hashlib,ssl,time,urllib.error,urllib.request
from urllib.parse import urlsplit
from sources import guard
class FetchFailure(Exception):
 def __init__(self,msg,retry=0):super().__init__(msg);self.retry=retry
class SameHost(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,hdrs,newurl):
  a,b=urlsplit(req.full_url),urlsplit(newurl);guard(b.scheme=='https' and a.hostname==b.hostname and b.username is None and b.password is None,'redirect outside registered publisher');return super().redirect_request(req,fp,code,msg,hdrs,newurl)
def delay(v,now=None):
 if not v:return 0
 try:return max(0,int(v))
 except ValueError:
  try:return max(0,int(email.utils.parsedate_to_datetime(v).timestamp()-(now or time.time())+1))
  except (TypeError,ValueError,OverflowError):return 0
def fetch(url,post=None):
 cap=5*1024*1024;h={'User-Agent':'MarketLens-Auto/6.8 (public economic data; paced polling)','Accept-Encoding':'identity'}
 if post:h['Content-Type']='application/json'
 req=urllib.request.Request(url,data=post,headers=h);guard(urlsplit(url).scheme=='https','HTTPS required');started=time.monotonic()
 try:
  with urllib.request.build_opener(SameHost()).open(req,timeout=16) as r:
   guard(r.status==200 and urlsplit(r.url).hostname==urlsplit(url).hostname,'unexpected HTTP status/origin');n=r.headers.get('Content-Length');guard(n is None or 0<int(n)<=cap,'invalid or oversized Content-Length');buf=bytearray()
   while True:
    guard(time.monotonic()-started<45,'total receive deadline exceeded');b=r.read(65536)
    if not b:break
    buf.extend(b);guard(len(buf)<=cap,'body too large')
   guard(buf and (n is None or len(buf)==int(n)),'empty/truncated body');return bytes(buf),r.url,r.headers.get('ETag',''),r.headers.get('Last-Modified','')
 except urllib.error.HTTPError as e:
  try:raise FetchFailure('HTTP '+str(e.code)+' from '+urlsplit(url).hostname,delay(e.headers.get('Retry-After')))
  finally:e.close()
