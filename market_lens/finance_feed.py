"""OECD monthly market indicators. Window alignment is by calendar month, never by row number."""
import csv,io,math,statistics
from decimal import Decimal as D
AREAS={'ESP':'ES','DEU':'DE','FRA':'FR','ITA':'IT','GBR':'GB','USA':'US','CAN':'CA','JPN':'JP','CHN':'CN','EA20':'EA'};FX=('EA20','CAN','GBR','JPN','CHN','USA');EU=('EA','ES','DE','FR','IT');FLOW='OECD.SDD.STES,DSD_STES@DF_FINMARK,4.0';K='oecd-fin';MEAS=('IRLT','IR3TIB','SHARE');FN=('curve','vol12','beta24');ID=lambda a,key:'FIN.'+a+'.'+key

def register(feed,metric):
 feed(K,'OECD','Monthly yields, rate spreads and equity–yield sensitivity',120,60,'Markets')
 for a,g in AREAS.items():
  country=' · EA20' if a=='EA20' else '';m=lambda key,label,unit,factor,relation:metric(K,ID(a,key),label+country,unit,'M',EU if key=='fx3m' and a=='EA20' else g,factor,relation)
  m('IRLT','Long-term benchmark interest rate','% per annum','YLD-01','Monthly OECD edition; not a daily executable yield');m('IR3TIB','Three-month interbank indicator','% per annum','','OECD input; do not carry a discontinued rate');m('SHARE','Broad share-price index','index, 2015=100','','Price-only OECD input, not a reinvested total-return index');m('curve','Long yield minus 3-month money rate','percentage points','YLD-03','Same-month difference, not a traded spread');m('vol12','Volatility of monthly long-yield changes','basis points','YLD-06','Sample SD of 12 consecutive monthly changes; not annualized');m('beta24','Share-price sensitivity to long-yield changes','% log price change per pp','IDX-04','OLS slope with intercept, 24 consecutive monthly changes; proxy, not an expected return')
  if a in FX:m('fx3m','Three-month forward premium estimate','% over three months','FX-05','Interest-parity estimate vs '+('EA20' if a=='USA' else 'USA')+'; not a forward quote; shared for euro members')

def request(d):return 'https://sdmx.oecd.org/public/rest/v1/data/'+FLOW+'/ESP+DEU+FRA+ITA+GBR+USA+CAN+JPN+CHN+EA20.M.IRLT+IR3TIB+SHARE......?startPeriod='+str(d.year-5)+'-01&format=csvfilewithlabels',None

def fill(o,text,day):
 from sources import mn,month,guard,checks
 lo=mn(str(day.year-5)+'-01');obs={};ends={};presence=set()
 for n,r in enumerate(csv.DictReader(io.StringIO(text)),2):
  a=r['REF_AREA'];key=r['MEASURE'];guard(a in AREAS and key in MEAS,'wrong OECD financial measure/area');checks(r,dict(STRUCTURE='DATAFLOW',STRUCTURE_ID='OECD.SDD.STES:DSD_STES@DF_FINMARK(4.0)',ACTION='I',FREQ='M',UNIT_MEASURE='IX' if key=='SHARE' else 'PA',ACTIVITY='_Z',ADJUSTMENT='_Z',TRANSFORMATION='_Z',TIME_HORIZ='_Z',METHODOLOGY='N',UNIT_MULT='0',BASE_PER='2015' if key=='SHARE' else ''));t=r['TIME_PERIOD'];j=mn(t);guard(lo<=j and month(j)==t and (a,key,j) not in obs,'OECD financial period/duplicate');flag=r['OBS_STATUS'];v=r['OBS_VALUE'];guard(flag in ('A','B','P','E','M'),'unknown OECD financial status');x=None if v in ('','NaN',':') else D(v);guard((x is None)==(flag=='M') and (x is None or x.is_finite() and (key!='SHARE' or x>0)),'OECD financial value/status');obs[a,key,j]=(x,flag,n);ends[a]=max(ends.get(a,j),j);presence.add((a,key))
 guard(len(ends)>=10 and all((a,'IRLT') in presence and (a,'SHARE') in presence for a in ends),'incomplete area/level set');
 def make(a,key,j,legs,calc,offset=0):
  data=[obs.get(z,(None,'input absent',None)) for z in legs];xs=[u[0] for u in data];flag='|'.join(sorted({u[1] for u in data if u[1]!='A'}));v=calc(xs) if all(x is not None for x in xs) else None
  if v is None and not flag:flag='insufficient variation'
  locator=','.join(str(u[2] or '-') for u in data);i=ID(a,key);o.put(i,month(j),v,flag or 'A',at=key+': CSV records '+locator);o.window[i]=month(lo+offset)
 for a,top in ends.items():
  guard(top-lo>=24,'short financial baseline');
  for j in range(lo,top+1):
   for k in MEAS:make(a,k,j,[(a,k,j)],lambda xs:xs[0]);
   make(a,'curve',j,[(a,'IRLT',j),(a,'IR3TIB',j)],lambda xs:xs[0]-xs[1]);
   if j>=lo+12:make(a,'vol12',j,[(a,'IRLT',q) for q in range(j-12,j+1)],lambda xs:round(100*statistics.stdev(float(y-x) for x,y in zip(xs,xs[1:])),10),12)
   if j>=lo+24:
    def beta(xs):
     rates=list(map(float,xs[:25]));idx=list(map(float,xs[25:]));dx=[b-a for a,b in zip(rates,rates[1:])];dy=[100*math.log(b/a) for a,b in zip(idx,idx[1:])];return round(statistics.covariance(dx,dy)/statistics.variance(dx),10) if statistics.variance(dx)>1e-7 else None
    make(a,'beta24',j,[(a,k,q) for k in ('IRLT','SHARE') for q in range(j-24,j+1)],beta,24)
  if a in FX:
   ref='EA20' if a=='USA' else 'USA'
   for j in range(lo,max(ends[a],ends[ref])+1):make(a,'fx3m',j,[(a,'IR3TIB',j),(ref,'IR3TIB',j)],lambda x:(100*((400+x[0])/(400+x[1])-1)).quantize(D('.0000000001')) if x[1]>-400 else None)
 return o.finish(0)
