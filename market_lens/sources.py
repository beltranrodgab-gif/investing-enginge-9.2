"""Official-source registry and strict parser contracts. Live values are separate from any cutoff edition."""
from __future__ import annotations
import calendar,collections,csv,datetime as dt,decimal,html,io,itertools,json,math,re,xml.etree.ElementTree as ET
from urllib.parse import urlencode
D=decimal.Decimal;UTC=dt.timezone.utc;VERS='6.8';G={'ES':'Spain','EA':'Euro area','DE':'Germany','FR':'France','IT':'Italy','GB':'United Kingdom','US':'United States','CA':'Canada','JP':'Japan','CN':'China','AU':'Australia','KR':'South Korea','CH':'Switzerland','SE':'Sweden','NO':'Norway','MX':'Mexico','BR':'Brazil'};EU=('EA','ES','DE','FR','IT');ISO={'CAN':'CA','CHN':'CN','JPN':'JP','USA':'US','XM':'EA','EA21':'EA'};O='https://www.ons.gov.uk';SC='https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorByReferencePeriodRange';BIS='https://stats.bis.org/api/v1/data/';OE='https://sdmx.oecd.org/public/rest/v1/data/';EURO='https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/';BLS='https://api.bls.gov/publicAPI/v2/timeseries/data/';F={};M={}
def guard(ok,msg):
 if not ok:raise ValueError(msg)
def feed(k,org,title,minutes,minimum,group):F[k]=dict(key=k,publisher=org,title=title,minutes=minutes,min_minutes=minimum,group=group)
def metric(k,i,label,unit,freq,geos,factor='',relation='Atlas measure',days=None):
 guard(i not in M,'duplicate metric');M[i]=dict(id=i,feed=k,label=label,unit=unit,freq=freq,geos=list(geos) if not isinstance(geos,str) else [geos],factor=factor,relation=relation,tail_days=days or {'D':8,'M':95,'Q':200}[freq]);return i
feed('ecb','European Central Bank','Euro reference fixings',30,15,'Markets');CC={'CAD':'CA','CNY':'CN','GBP':'GB','JPY':'JP','USD':'US'}
for c,g in CC.items():metric('ecb','ECB.'+c,'Euro reference fixing · '+c+' per EUR',c+' per EUR','D',(*EU,'US') if c=='USD' else g,'FX-01','Shared quote; not executable' if c=='USD' else 'Reference rate; not executable',6)
# id, CDID, dataset, identity title, first native period, cadence, series label, units, factor, path
ONS={
 'uk-cpi':('D7G7','MM23','CPI ANNUAL RATE 00: ALL ITEMS 2015=100','1989-01','M','Consumer inflation · all items','% year on year','INF-01','economy/inflationandpriceindices'),
 'uk-ppi':('GB7S','PPI','PPI INDEX OUTPUT DOMESTIC - C Manufactured products, excluding Duty 2015=100','1957-01','M','Manufactured output prices · excluding duty','index, 2015=100','','economy/inflationandpriceindices'),
 'uk-steel':('EW6T','PPI','PPI INDEX OUTPUT DOMESTIC - C2410 Basic iron and steel and ferro-alloys 2015=100','1996-01','M','Producer price · basic steel and ferro-alloys','index, 2015=100','COM-06','economy/inflationandpriceindices'),
 'uk-wood':('EVUD','PPI','PPI INDEX OUTPUT DOMESTIC - C1610 Wood, sawn and planed 2015=100','1996-01','M','Producer price · sawn and planed wood','index, 2015=100','COM-10','economy/inflationandpriceindices'),
 'uk-fertiliser':('EVXG','PPI','PPI INDEX OUTPUT DOMESTIC - C2015 Fertilisers and nitrogen compounds 2015=100','2008-12','M','Producer price · fertilisers and nitrogen compounds','index, 2015=100','COM-11','economy/inflationandpriceindices'),
 'uk-gdp':('IHYQ','PN2','Gross Domestic Product: Quarter on Quarter growth: CVM SA %','1955-Q2','Q','Real GDP growth · seasonally adjusted','% quarter on quarter','ACT-01','economy/grossdomesticproductgdp')}
for k,(cid,ds,title,start,fr,label,unit,f,path) in ONS.items():feed(k,'Office for National Statistics',label,60,30,'Economy');metric(k,'ONS.'+cid,label,unit,fr,'GB',f,'Input level' if not f else 'Atlas measure')
metric('uk-ppi','ONS.GB7S.YOY','Manufactured output price inflation · excluding duty','% year on year','M','GB','INF-03','Calculated from levels exactly 12 months apart')
# Vector/product/coordinate checked against StatCan getSeriesInfoFromVector. Scope & base fixed; never identify a vector by free text.
V={'41690973':('ca-cpi',18100004,'2.2',1,'All-items consumer price index','index, 2002=100',''), '803313':('ca-inv',16100047,'1.8.2.1',2,'Manufacturing inventory-to-sales ratio · SA','ratio','SUP-05'), '1568389346':('ca-jobs',14100442,'1.1.1',0,'Job vacancies · all industries','vacancies','LAB-05'), '1230995983':('ca-prices',18100265,'1.1',1,'Industrial product prices · headline','index, Jan 2020=100','INF-03'), '1230995994':('ca-prices',18100265,'1.12',1,'Producer price · lumber and other wood products','index, Jan 2020=100','COM-10'), '1230995997':('ca-prices',18100265,'1.15',1,'Producer price · primary ferrous metals','index, Jan 2020=100','COM-06'), '1230996179':('ca-prices',18100266,'1.173',1,'Producer price · unwrought nickel and alloys','index, Jan 2020=100','COM-07'), '1230998150':('ca-prices',18100268,'1.16',1,'Raw material price · wheat','index, Jan 2020=100','COM-09'), '1324952493':('ca-prices',18100266,'1.358',1,'Producer price · mixed fertilizers','index, Jan 2020=100','COM-11'), '1324952502':('ca-prices',18100266,'1.367',1,'Producer price · diesel fuel','index, Jan 2020=100','COM-02')}
for k,t in [('ca-cpi','Consumer prices'),('ca-inv','Manufacturing inventories'),('ca-jobs','Quarterly job vacancies'),('ca-prices','Industrial and raw-material prices')]:feed(k,'Statistics Canada',t,60,30,'Economy')
for v,(k,prod,coord,dec,label,unit,f) in V.items():metric(k,'SC.'+v,label,unit,'Q' if k=='ca-jobs' else 'M','CA',f,'Input level' if not f else 'Atlas measure',220 if k=='ca-jobs' else None)
metric('ca-cpi','SC.CPI.YOY','Consumer inflation · all items','% year on year','M','CA','INF-01','National-source alternative; calculated from index levels, not spliced with OECD')
feed('ca-bank','Bank of Canada','Policy, repo volume, balances & bond yield',30,15,'Markets');BANK={'V39079':('Target for the overnight rate (business daily)','Percent','Overnight policy target','%','MP-01'),'CORRA_TOTAL_VOLUME':('CORRA: total dollar trading volume of all trades eligible','Canadian dollars (CAD)','CORRA-eligible repo volume · before trimming','CAD','MON-05'),'ACTUAL':('Amount in Lynx','Millions of Canadian dollars (CAD)','Settlement balances in Lynx · subject to replacement','CAD millions','MON-03'),'BD.CDN.RRB.DQ.YLD':('Real Return Bonds yield, long-term','Percent','Long-term real return bond yield','%','YLD-02')}
for key,(_,_,label,unit,f) in BANK.items():metric('ca-bank','BOC.'+key,label,unit,'D','CA',f,'Direct national source' if key=='V39079' else 'Atlas measure')
feed('ny-sofr','Federal Reserve Bank of New York','Secured Overnight Financing Rate',30,15,'Markets');metric('ny-sofr','NYFED.SOFR','Secured Overnight Financing Rate','%','D','US','MON-05','Administered rate, not an executable quote')
EUQ={'eu-hicp':('prc_hicp_minr',dict(freq='M',unit='RCH_A',coicop18='TOTAL'),'HICP inflation · all items','% year on year','INF-01'), 'eu-gdp':('namq_10_gdp',dict(freq='Q',unit='CLV_PCH_PRE',s_adj='SCA',na_item='B1GQ'),'Real GDP growth · seasonally and calendar adjusted','% quarter on quarter','ACT-01'), 'eu-jobs':('une_rt_m',dict(freq='M',s_adj='SA',age='TOTAL',unit='PC_ACT',sex='T'),'Unemployment · all persons, seasonally adjusted','% of labour force','LAB-02')};EUG=('DE','EA21','ES','FR','IT')
for k,(code,dims,t,unit,f) in EUQ.items():
 feed(k,'Eurostat',t,60,30,'Economy');
 for g in EUG:metric(k,code+'.'+g,t+(' · EA21' if g=='EA21' else ''),unit,dims['freq'],ISO.get(g,g),f)
SD={
'bis-eer':('WS_EER','M.R.B.ES+DE+FR+IT+GB+US+CA+JP+CN+XM',dict(FREQ='M',EER_TYPE='R',EER_BASKET='B',UNIT_MEASURE='882',OBS_CONF='F'),('ES','DE','FR','IT','GB','US','CA','JP','CN','XM'),'Broad real effective exchange rate','index, 2020=100','FX-02'),
'bis-cpi':('WS_LONG_CPI','M.CN+US.771',dict(FREQ='M',UNIT_MEASURE='771',UNIT_MULT='0',OBS_CONF='F'),('CN','US'),'Consumer inflation · all items, BIS edition','% year on year','INF-01'),
'bis-rates':('WS_CBPOL','D.CA+CN+GB+JP+US+XM',dict(FREQ='D',UNIT_MEASURE='368',UNIT_MULT='0',OBS_CONF='F'),('CA','CN','GB','JP','US','XM'),'Central bank policy rate · BIS convention','% per annum','MP-01'),
'oecd-cpi':('OECD.SDD.TPS,DSD_PRICES_COICOP2018@DF_PRICES_C2018_ALL,1.0','CAN+JPN.M.N.CPI.PA._T.N.GY',dict(FREQ='M',MEASURE='CPI',METHODOLOGY='N',UNIT_MEASURE='PA',EXPENDITURE='_T',ADJUSTMENT='N',TRANSFORMATION='GY'),('CAN','JPN'),'Consumer inflation · all items, OECD edition','% year on year','INF-01'),
'oecd-gdp':('OECD.SDD.NAD,DSD_NAMAIN1@DF_QNA_EXPENDITURE_GROWTH_OECD,1.1','Q.Y.CAN+CHN+JPN+USA.S1.S1.B1GQ._Z._Z._Z.PC.L.G1.T0102',dict(FREQ='Q',ADJUSTMENT='Y',SECTOR='S1',COUNTERPART_SECTOR='S1',TRANSACTION='B1GQ',INSTR_ASSET='_Z',ACTIVITY='_Z',EXPENDITURE='_Z',UNIT_MEASURE='PC',PRICE_BASE='L',TRANSFORMATION='G1',TABLE_IDENTIFIER='T0102',CONF_STATUS='F',UNIT_MULT='0'),('CAN','CHN','JPN','USA'),'Real GDP growth · seasonally adjusted','% quarter on quarter','ACT-01'),
'oecd-jobs':('OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0','CAN+JPN+USA.UNE_LF_M.PT_LF_SUB._Z.Y._T.Y_GE15._Z.M',dict(FREQ='M',MEASURE='UNE_LF_M',UNIT_MEASURE='PT_LF_SUB',TRANSFORMATION='_Z',ADJUSTMENT='Y',SEX='_T',AGE='Y_GE15',ACTIVITY='_Z',UNIT_MULT='0'),('CAN','JPN','USA'),'Unemployment · age 15+, seasonally adjusted','% of labour force','LAB-02')}
for k,(flow,sel,dims,geos,t,unit,f) in SD.items():
 feed(k,'BIS' if k.startswith('bis-') else 'OECD',t,120,60,'Markets' if k in ('bis-rates','bis-eer') else 'Economy');
 for g in geos:
  metric(k,k+'.'+g,t,unit,dims['FREQ'],EU if k=='bis-rates' and g=='XM' else ISO.get(g,g),f,'Shared euro-area policy; distributor edition' if k=='bis-rates' and g=='XM' else 'Distributor edition',16 if k=='bis-rates' else None)
feed('us-bls','U.S. Bureau of Labor Statistics','Consumer prices & unemployment',360,360,'Economy');metric('us-bls','BLS.CUUR0000SA0','CPI-U · all items, US city average, not seasonally adjusted','index, 1982–84=100','M','US','','Input level');metric('us-bls','BLS.CPI.YOY','Consumer inflation · CPI-U all items','% year on year','M','US','INF-01','National-source alternative; calculated from levels');metric('us-bls','BLS.LNS14000000','Unemployment · civilian labour force, 16+, SA','% of labour force','M','US','LAB-02','National-source alternative, distinct from OECD series')

import finance_feed,market_feed
finance_feed.register(feed,metric)
market_feed.register(feed,metric)
market_feed.register_ust(feed,metric)

def month(i):return f'{i//12:04d}-{i%12+1:02d}'
def mn(t):return int(t[:4])*12+int(t[-2:])-1
def span_start(k,d):
 if k in ('ca-bank','bis-rates'):return (d-dt.timedelta(days=120)).isoformat()
 if k in ('eu-gdp','oecd-gdp'):return f'{d.year-3}-Q1'
 if k in ('eu-jobs','oecd-jobs','bis-cpi','us-bls'):return f'{d.year-2}-01'
 return month(mn(d.strftime('%Y-%m'))-30)
def quarter_floor_start(lo):
 j=mn(lo);j+=(-j)%3;t=month(j);return t[:4]+'-Q'+str((int(t[-2:])-1)//3+1)

def request(k,d):
 if k=='oecd-fin':return finance_feed.request(d)
 if k=='ecb-daily-fx':return market_feed.request(d)
 if k=='us-treasury-daily':return market_feed.request_ust(d)
 lo=span_start(k,d)
 if k=='ecb':return 'https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml',None
 if k in ONS:c,ds,*_=ONS[k];return f'{O}/{ONS[k][-1]}/timeseries/{c.lower()}/{ds.lower()}/data',None
 if k in ('ca-prices','ca-cpi','ca-inv','ca-jobs'):return SC+'?'+urlencode(dict(vectorIds=','.join('"'+v+'"' for v,r in V.items() if r[0]==k),startRefPeriod=lo+'-01',endReferencePeriod=d.isoformat())),None
 if k=='ca-bank':return 'https://www.bankofcanada.ca/valet/observations/'+','.join(BANK)+'/json?start_date='+lo,None
 if k=='ny-sofr':return 'https://markets.newyorkfed.org/api/rates/secured/sofr/last/60.json',None
 if k in EUQ:
  code,dim,*_=EUQ[k];return EURO+code+'?'+urlencode([('geo',g) for g in EUG]+list(dim.items())+[('sinceTimePeriod',lo),('lang','EN')]),None
 if k in SD:
  flow,sel,*_=SD[k];isbis=k.startswith('bis-');return (BIS if isbis else OE)+flow+'/'+sel+'?'+urlencode(dict(startPeriod=lo,**({'endPeriod':d.isoformat()} if k=='bis-rates' else {}),format='csv' if isbis else 'csvfile')),None
 if k=='us-bls':return BLS,json.dumps({'seriesid':['CUUR0000SA0','LNS14000000'],'startyear':str(d.year-2),'endyear':str(d.year)}).encode()
 raise KeyError(k)
def end(t,fr):
 if fr=='D':a=dt.date.fromisoformat(t);guard(a.isoformat()==t,'bad ISO date');return a
 if fr=='M':guard(re.fullmatch(r'\d{4}-(?:0[1-9]|1[012])',t),'bad month');y,m=map(int,t.split('-'))
 else:guard(re.fullmatch(r'\d{4}-Q[1-4]',t),'bad quarter');y,m=int(t[:4]),3*int(t[-1])
 return dt.date(y,m,calendar.monthrange(y,m)[1])
def num(v):
 x=D(str(v));guard(x.is_finite() and abs(x)<D('1e25'),'invalid non-finite/out-of-range value');return format(x.normalize(),'f') if x else '0'
def checks(r,dims):guard(all(str(r.get(k))==str(v) for k,v in dims.items()),'wrong series dimensions: '+','.join(k for k,v in dims.items() if str(r.get(k))!=str(v)))
def seq(ts,fr):
 a=[mn(t) if fr=='M' else int(t[:4])*4+int(t[-1]) for t in sorted(ts)];return all(y==x+1 for x,y in zip(a,a[1:]))
def easter(y):
 a=y%19;b,c=divmod(y,100);d,e=divmod(b,4);f=(b+8)//25;g=(b-f+1)//3;h=(19*a+b-d-g+15)%30;i,k=divmod(c,4);l=(32+2*e+2*i-h-k)%7;m=(a+11*h+22*l)//451;mo,j=divmod(h+l-7*m+114,31);return dt.date(y,mo,j+1)
def target(d):e=easter(d.year);return d.weekday()<5 and (d.month,d.day) not in ((1,1),(5,1),(12,25),(12,26)) and d not in (e-dt.timedelta(days=2),e+dt.timedelta(days=1))
def ca_release(t,seen):
 # Publisher time is Eastern (DST observed). WDS daily releases are normally 08:30 local.
 wall=dt.datetime.fromisoformat(t);guard(wall.tzinfo is None and wall.year>=2007,'unsupported WDS clock');y=wall.year;sun=lambda m:1+(6-dt.date(y,m,1).weekday())%7;dst=dt.datetime(y,3,sun(3)+7,2)<=wall<dt.datetime(y,11,sun(11),2);off=dt.timezone(dt.timedelta(hours=-4 if dst else -5));actual=wall.replace(tzinfo=off);guard(actual<=seen,'future WDS release');return actual.isoformat()
class Snapshot:
 def __init__(self,k,seen):self.k=k;self.seen=seen;self.cells={};self.hint='';self.window={}
 def put(self,i,t,v,flag='',pub='',at=''):
  m=M[i]
  if v is not None:
   x=D(str(v));guard(x.is_finite(),'nonfinite source value');guard(not (m['factor']=='LAB-02' and not 0<=x<=100),'unemployment outside 0..100');guard(not (('index' in m['unit'].lower() or i in ('SC.41690973','BLS.CUUR0000SA0')) and x<=0),'non-positive level index');guard(not (i=='BOC.CORRA_TOTAL_VOLUME' and x<0),'negative repo volume')
  guard(m['feed']==self.k and end(t,m['freq'])<=self.seen.date() and (i,t) not in self.cells,'future, duplicate or wrong observation: '+i+' '+t);self.cells[i,t]=(None if v is None else num(v),str(flag or ''),str(pub or ''),str(at))
 def yoy(self,base,dst):
  for i,t in list(self.cells):
   if i!=base:continue
   old=month(mn(t)-12)
   if (base,old) not in self.cells:continue
   a=self.cells[base,old];b=self.cells[i,t];x=None if a[0] is None or b[0] is None else (D(b[0])/D(a[0])-1)*100;self.put(dst,t,None if x is None else x.quantize(D('.000001')), '|'.join(sorted(set(filter(None,(a[1],b[1]))))),max(a[2],b[2]),base+': '+old+' & '+t+'; 100*(new/old−1)')
 def finish(self,minimum):
  got=collections.defaultdict(list)
  for (i,t),v in self.cells.items():got[i].append((t,v[0]))
  guard(set(got)=={i for i,m in M.items() if m['feed']==self.k},'series absent or unexpected');
  for i,rows in got.items():
   lo=min(t for t,_ in rows);self.window.setdefault(i,lo);guard(sum(v is not None for t,v in rows)>=minimum,'short/empty baseline: '+i)
  return self

def parse(k,b,seen,request_day=None):
 d=request_day or seen.date();o=Snapshot(k,seen);text=b.decode('utf-8-sig');J=lambda:json.loads(text,parse_float=D);lowest=span_start(k,d)
 if k=='oecd-fin':return finance_feed.fill(o,text,d)
 if k=='ecb-daily-fx':return market_feed.fill(o,text,d)
 if k=='us-treasury-daily':return market_feed.fill_ust(o,text,d)
 if k=='ecb':
  ns={'e':'http://www.ecb.int/vocabulary/2002-08-01/eurofxref','g':'http://www.gesmes.org/xml/2002-08-01'};tree=ET.fromstring(b);guard(tree.findtext('g:Sender/g:name',namespaces=ns)=='European Central Bank','ECB sender');ts=set()
  for day in tree.findall('e:Cube/e:Cube',ns):
   t=day.attrib['time'];guard(t not in ts,'duplicate ECB fixing');ts.add(t);cc=set()
   for r in day.findall('e:Cube',ns):
    c=r.attrib['currency']
    if c in CC:guard(D(r.attrib['rate'])>0,'non-positive fixing');o.put('ECB.'+c,t,r.attrib['rate'],at='Cube['+t+']/Cube['+c+']/@rate');cc.add(c)
   guard(cc==CC.keys(),'ECB currency missing')
  guard(ts,'empty ECB');lo=dt.date.fromisoformat(min(ts));hi=dt.date.fromisoformat(max(ts));n=(hi-lo).days;guard(84<=n<=96 and ts=={a.isoformat() for i in range(n+1) if target(a:=lo+dt.timedelta(days=i))},'ECB fixing calendar gap');return o.finish(40)
 if k in ONS:
  cid,ds,title,start,fr,*_=ONS[k];j=J();h=j['description'];checks(h,dict(cdid=cid,datasetId=ds,title=title));arr='months' if fr=='M' else 'quarters';ts={};i='ONS.'+cid;o.hint=h['releaseDate'];rd=dt.datetime.fromisoformat(o.hint.replace('Z','+00:00'));guard(rd.tzinfo is not None and rd<=seen,'future ONS edition')
  for n,r in enumerate(j[arr]):
   if fr=='M':mm=tuple(calendar.month_name).index(r['month']);t=f'{int(r["year"]):04d}-{mm:02d}';label=r['year']+' '+calendar.month_abbr[mm].upper()
   else:t=r['year']+'-'+r['quarter'];label=r['year']+' '+r['quarter']
   guard(r['date']==label and r['sourceDataset']==ds,'wrong ONS period or series');v=r['value'];guard(v not in (None,''),'unexpected missing ONS cell');guard(ds!='PPI' or D(v)>0,'nonpositive index');o.put(i,t,v,at=f'{arr}[{n}].value');ts[t]=label
  tail=max(ts);guard(min(ts)==start and seq(ts,fr) and ts[tail]==h['date'] and D(h['number'])==D(o.cells[i,tail][0]) and end(tail,fr)<=rd.date(),'ONS baseline/edition mismatch');
  if k=='uk-ppi':o.yoy(i,'ONS.GB7S.YOY')
  return o.finish(24)
 if k in ('ca-cpi','ca-prices','ca-inv','ca-jobs'):
  vectors={v for v,r in V.items() if r[0]==k};matched=set()
  for idx,j in enumerate(J()):
   guard(j['status']=='SUCCESS','WDS error');a=j['object'];v=str(a['vectorId']);guard(v in vectors and v not in matched,'wrong WDS vector');matched.add(v);_,prod,coord,deci,*_=V[v];fullcoord='.'.join(coord.split('.')+['0']*(10-len(coord.split('.'))));checks(a,dict(productId=prod,coordinate=fullcoord,responseStatusCode=0));quarter=k=='ca-jobs';ts=[]
   for n,r in enumerate(a['vectorDataPoint']):
    checks(r,dict(frequencyCode=9 if quarter else 6,decimals=deci,scalarFactorCode=0,refPer2=''));t=r['refPer'][:7];guard(r['refPer']==t+'-01' and t>=lowest,'WDS month mismatch');
    if quarter:guard(int(t[-2:]) in (1,4,7,10),'quarter mislabeled as month');t=t[:4]+'-Q'+str((int(t[-2:])-1)//3+1)
    val=r['value'];st=int(r['statusCode']);sec=int(r['securityLevelCode']);guard(sec==0 or val is None,'unexpected non-public numeric WDS row');guard(st in range(9) and (val is None or st not in (1,2,8)),'unknown/invalid WDS status/value');guard(val is None or D(str(val))>=0,'invalid WDS level');flag={0:'',3:'A',4:'B',5:'C',6:'D',7:'E',8:'F'}.get(st,'status:'+str(st));flag+=(';symbol:'+str(r['symbolCode'])) if r['symbolCode'] else '';stamp=ca_release(r['releaseTime'],seen);o.put('SC.'+v,t,val,flag,stamp,f'object[{idx}].vectorDataPoint[{n}] v{v}');ts.append(t)
   guard(seq(ts,'Q' if quarter else 'M'),'unexplained internal WDS gap');o.window['SC.'+v]=lowest if not quarter else quarter_floor_start(lowest)
  guard(matched==vectors,'WDS vector missing');
  if k=='ca-cpi':o.yoy('SC.41690973','SC.CPI.YOY');o.window['SC.CPI.YOY']=month(mn(lowest)+12)
  return o.finish(4 if k=='ca-jobs' else 12)
 if k=='ca-bank':
  j=J();guard(set(j['seriesDetail'])==BANK.keys(),'Valet identity');
  for c,(label,unit,*_) in BANK.items():a=j['seriesDetail'][c];guard(a['label']==label and a['metadata']['unit']==unit and a['metadata']['frequency']=='Business daily','Valet label/unit');o.window['BOC.'+c]=lowest
  days=set()
  for n,a in enumerate(j['observations']):
   t=a['d'];guard(t not in days and t>=lowest,'duplicate/out-of-window Valet day');days.add(t)
   for c in BANK:
    if c in a:o.put('BOC.'+c,t,a[c].get('v'),at=f'observations[{n}].{c}.v')
  return o.finish(35)
 if k=='ny-sofr':
  rates=J()['refRates'];guard(len(rates)==60,'incomplete last-60, possible silently removed days');dates=[]
  for n,r in enumerate(rates):
   guard(r['type']=='SOFR','NY Fed rate type');dates.append(r['effectiveDate']);qq=[D(str(r[z])) for z in ('percentPercentile1','percentPercentile25','percentRate','percentPercentile75','percentPercentile99')];guard(qq==sorted(qq) and D(str(r['volumeInBillions']))>=0,'SOFR percentile / volume mismatch');o.put('NYFED.SOFR',r['effectiveDate'],r['percentRate'],r.get('revisionIndicator',''),at=f'refRates[{n}].percentRate')
  guard(dates==sorted(dates,reverse=True),'NY Fed window order');return o.finish(60)
 if k in EUQ:
  code,dim,*_=EUQ[k];j=J();checks(j,{'source':'ESTAT','class':'dataset','version':'2.0'});checks(j['extension'],{'agencyId':'ESTAT','id':code.upper()});checks(j['extension']['datastructure'],{'agencyId':'ESTAT','id':code.upper()});ids=j['id'];shape=j['size'];guard(len(set(ids))==len(ids) and set(ids)==set(dim)|{'geo','time'} and len(shape)==len(ids),'Eurostat dimension schema');idx={};o.hint=j.get('updated','');guard(o.hint and dt.datetime.fromisoformat(o.hint)<=seen,'future Eurostat update')
  for x,n in zip(ids,shape):
   cats=j['dimension'][x]['category']['index'];guard(isinstance(cats,dict) and n>0 and sorted(cats.values())==list(range(n)),'Eurostat coordinate mapping');idx[x]=sorted(cats,key=cats.get)
  guard(all(idx[x]==[v] for x,v in dim.items()) and set(idx['geo'])==set(EUG),'Eurostat selector/coverage');values=j['value'];flags=j.get('status',{});get=lambda arr,n:arr.get(str(n)) if isinstance(arr,dict) else arr[n] if n<len(arr) else None
  cells=math.prod(shape);guard(all(type(n) is int and n>0 for n in shape) and cells<=50000,'invalid grid shape')
  for arr in (values,flags):guard((isinstance(arr,list) and len(arr)==cells) or (isinstance(arr,dict) and all(str(z).isdigit() and str(int(z))==z and 0<=int(z)<cells for z in arr)),'truncated or out-of-range JSON-stat grid')
  for n,key in enumerate(itertools.product(*(idx[x] for x in ids))):
   row=dict(zip(ids,key));i=code+'.'+row['geo'];t=row['time'];guard(t>=lowest,'old Eurostat response range');value=get(values,n);flag=get(flags,n);o.window[i]=lowest
   # JSON-stat may list a month on its shared axis with NO cell for any selected geography.
   # Absent coordinates are not releases. Explicit nulls/flags remain missing, never zero.
   if value is None and not flag and isinstance(values,dict) and str(n) not in values:continue
   guard(value is None or not set(str(flag or '').lower().split()) & {'c','z',':'},'non-public/not applicable Eurostat cell with numeric value')
   o.put(i,t,value,flag or '',at=f'JSON-stat value[{n}], {row["geo"]}/{t}')
  return o.finish(6)
 if k in SD:
  flow,sel,dims,geos,*_=SD[k];oecd=k.startswith('oecd-');
  for n,r in enumerate(csv.DictReader(io.StringIO(text)),2):
   checks(r,dims);ar=r['REF_AREA'];t=r['TIME_PERIOD'];guard(ar in geos and t>=lowest,'out-of-scope SDMX key');
   if not oecd and 'UNIT_MULT' in r:guard(r['UNIT_MULT'] in ('','0'),'unexpected BIS scale')
   if k=='bis-eer':checks(r,dict(COLLECTION='A'))
   if oecd:org,ds,version=flow.split(',');checks(r,dict(DATAFLOW=org+':'+ds+'('+version+')'));guard(r.get('UNIT_MULT','') in ('','0'),'unexpected scale')
   v=r['OBS_VALUE'];flag=r.get('OBS_STATUS','');
   if v in ('','NaN',':'):guard(flag not in ('A','P'),'numeric cell absent despite value status');v=None
   else:guard(flag not in ('M','L'),'non-numeric status on value')
   i=k+'.'+ar;o.put(i,t,v,flag,at=f'CSV record {n}, {ar}/{t}');o.window[i]=lowest
  return o.finish(6 if dims['FREQ']=='Q' else 12)
 if k=='us-bls':
  j=J();guard(j['status']=='REQUEST_SUCCEEDED','BLS API failed: '+str(j.get('message',[]))[:100]);ids=set();
  for n,s in enumerate(j['Results']['series']):
   c=s['seriesID'];guard(c not in ids and c in ('CUUR0000SA0','LNS14000000'),'BLS unexpected series');ids.add(c);ts=[]
   for v,r in enumerate(s['data']):
    if r['period']=='M13':continue
    guard(re.fullmatch(r'M(0[1-9]|1[012])',r['period']) and r['periodName']==calendar.month_name[int(r['period'][1:])],'bad BLS period');t=r['year']+'-'+r['period'][1:];flag='; '.join(' '.join(filter(None,[x.get('code'),x.get('text')])) for x in r.get('footnotes',[]) if x);val=r['value'];o.put('BLS.'+c,t,val if val not in ('','-') else None,flag,at=f'Results.series[{n}].data[{v}]');ts.append(t)
   guard(seq(ts,'M') and min(ts)>=lowest,'BLS internal gap/coverage');o.window['BLS.'+c]=lowest
  guard(len(ids)==2,'BLS partial set');o.yoy('BLS.CUUR0000SA0','BLS.CPI.YOY');o.window['BLS.CPI.YOY']=month(mn(lowest)+12);return o.finish(6)
 raise ValueError('unsupported feed '+k)
