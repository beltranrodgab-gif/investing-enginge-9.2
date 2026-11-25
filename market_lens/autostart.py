"""Install/remove a user-owned start-at-login service. Never enables anything on download, needs an explicit enable command."""
import contextlib,datetime as dt,hashlib,os,plistlib,shutil,sqlite3,subprocess,sys,tempfile,time,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parent;TASK='Market Lens Auto 6.8';UNIT='market-lens-auto-68.service';LABEL='org.marketlens.auto68';CORE=('market_lens.py','sources.py','net.py','store.py','engine.py','dashboard.py','monitor.html','autostart.py','materialize.py','finance_feed.py','reliability.py','market_feed.py','strategy_adapter.py','strategy_engine.py','asset_classes.py','backtest.py','obsidian/__init__.py','obsidian/data/__init__.py','obsidian/data/contract.py','obsidian/data/licenses.py','obsidian/data/store.py','obsidian/data/validate.py','obsidian/data/service.py','obsidian/data/adapters/__init__.py','obsidian/data/adapters/base.py','obsidian/data/adapters/fred.py','obsidian/data/adapters/ecb.py','obsidian/data/adapters/yahoo.py','obsidian/data/adapters/marketlens.py','alpha_engine.py','macro_alerts.py','paper_trader.py','signal_filter.py','daily_processor.py','trade_signals.py','event_engine.py','stock_selector.py','jarvis_supervisor.py','plan_api.py','efficient_frontier.py','portfolio_optimizer.py','paper_account.py','yahoo_prices.py','research_store.py','market_data.py','sec_edgar.py','short_interest.py','research.py','quote_surface.py','analyst_consensus.py','delisted.py','delist_events.py');MARK='Market Lens Auto 6.8';HOME=Path.home()
def cmd(argv,check=True):
 r=subprocess.run(list(map(str,argv)),capture_output=True,text=True,errors='replace',timeout=55)
 if check and r.returncode:raise RuntimeError((r.stderr.strip() or r.stdout.strip() or 'service command failed')[:650])
 return r
def pathguard(x):
 s=str(x);assert '\x00' not in s
 if '\n' in s or '\r' in s:raise ValueError('service paths cannot contain a newline')
 return s
def linux_unit(args):
 q=lambda s:'"'+pathguard(s).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'
 return ('[Unit]\nDescription='+MARK+' — public macro release collector\nAfter=network.target\n\n[Service]\nType=simple\nExecStart='+' '.join(q(s) for s in args)+'\nRestart=on-failure\nRestartSec=60\nTimeoutStopSec=90\n\n[Install]\nWantedBy=default.target\n').encode()
def mac_agent(args):return plistlib.dumps(dict(Label=LABEL,ProgramArguments=list(map(str,args)),RunAtLoad=True,KeepAlive={'SuccessfulExit':False},ProcessType='Background',ThrottleInterval=60))
def win_task(args,user):
 ns='http://schemas.microsoft.com/windows/2004/02/mit/task';ET.register_namespace('',ns);q=lambda k:'{'+ns+'}'+k;root=ET.Element(q('Task'),version='1.2')
 def el(p,n,text=None,**atts):x=ET.SubElement(p,q(n),atts);x.text=text;return x
 inf=el(root,'RegistrationInfo');el(inf,'Description',MARK+' — user-scoped macro release collector');tr=el(el(root,'Triggers'),'LogonTrigger');el(tr,'Enabled','true');el(tr,'UserId',user);pid=el(el(root,'Principals'),'Principal',id='Author');el(pid,'UserId',user);el(pid,'LogonType','InteractiveToken');el(pid,'RunLevel','LeastPrivilege');s=el(root,'Settings')
 for k,v in [('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),('StopIfGoingOnBatteries','false'),('StartWhenAvailable','true'),('Enabled','true'),('ExecutionTimeLimit','PT0S')]:el(s,k,v)
 re=el(s,'RestartOnFailure');el(re,'Interval','PT1M');el(re,'Count','3');ac=el(el(root,'Actions',Context='Author'),'Exec');el(ac,'Command',str(args[0]));el(ac,'Arguments',subprocess.list2cmdline(list(map(str,args[1:]))));el(ac,'WorkingDirectory',str(Path(args[1]).resolve().parent));return ET.tostring(root,encoding='utf-16',xml_declaration=True)
def install_runtime(home):
 literal=b''.join(f.encode()+b'\0'+(ROOT/f).read_bytes()+b'\0' for f in CORE);name=hashlib.sha256(literal).hexdigest()[:16];dest=home/'runtime'/name;dest.mkdir(parents=True,exist_ok=True)
 for f in CORE:
  p=dest/f;p.parent.mkdir(parents=True,exist_ok=True)
  if p.exists() and p.read_bytes()!=(ROOT/f).read_bytes():raise ValueError('modified installed runtime. Choose a fresh data folder.')
  if not p.exists():shutil.copyfile(ROOT/f,p)
 return dest/'market_lens.py'
def ownwrite(path,data,tag):
 path.parent.mkdir(parents=True,exist_ok=True)
 if path.exists() and tag.encode() not in path.read_bytes():raise ValueError('refusing to replace unrelated login service: '+str(path))
 with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as f:f.write(data);tmp=Path(f.name)
 try:tmp.chmod(0o644);os.replace(tmp,path)
 finally:tmp.unlink(missing_ok=True)
def confirm_start(home,since,timeout=22):
 from store import db,when
 end=time.monotonic()+timeout
 while True:
  try:
   with contextlib.closing(db(home)) as c:r=c.execute('select started,beat,state from worker where single=1').fetchone()
   if r and r['state']=='running' and when(r['started'])>=since and 0<=(dt.datetime.now(dt.timezone.utc)-when(r['beat'])).total_seconds()<=45:return True
  except (ValueError,OSError,sqlite3.Error):pass
  if time.monotonic()>=end:raise RuntimeError('Auto-start was registered but a new worker heartbeat was not confirmed. Check worker.log in '+str(home)+', close any foreground worker, then enable again. Do not assume collection is running.')
  time.sleep(.4)
def main(action,home):
 home=Path(home).resolve();p=sys.platform;on=action=='enable';since=dt.datetime.now(dt.timezone.utc);py=Path(sys.executable).resolve();tag='Market Lens Auto 6.8';pathguard(home)
 if on:
  if sys.version_info<(3,11):raise ValueError('Python 3.11 or newer required')
  if p=='win32' and py.with_name('pythonw.exe').exists():py=py.with_name('pythonw.exe')
  args=[str(py),str(install_runtime(home)),'--data',str(home),'run']
 if p.startswith('linux'):
  if not shutil.which('systemctl'):raise RuntimeError('No systemd user manager; run python3 market_lens.py run in a persistent session or use your service manager.')
  cmd(['systemctl','--user','show','-p','Version','--value']);path=HOME/'.config/systemd/user'/UNIT
  if on:ownwrite(path,linux_unit(args),tag);cmd(['systemctl','--user','daemon-reload']);cmd(['systemctl','--user','enable',UNIT]);cmd(['systemctl','--user','restart',UNIT]);cmd(['systemctl','--user','is-active','--quiet',UNIT])
  elif path.exists():
   if tag.encode() not in path.read_bytes():raise RuntimeError('unrecognised service file')
   cmd(['systemctl','--user','disable','--now',UNIT]);path.unlink();cmd(['systemctl','--user','daemon-reload'])
 elif p=='darwin':
  where=f'gui/{os.getuid()}';path=HOME/'Library/LaunchAgents'/(LABEL+'.plist');cmd(['launchctl','print',where])
  if path.exists() and LABEL.encode() not in path.read_bytes():raise RuntimeError('unrecognised launch agent')
  if on:
   if path.exists():cmd(['launchctl','bootout',where+'/'+LABEL],False)
   ownwrite(path,mac_agent(args),LABEL);cmd(['launchctl','enable',where+'/'+LABEL]);cmd(['launchctl','bootstrap',where,str(path)])
  elif path.exists():
   loaded=cmd(['launchctl','print',where+'/'+LABEL],False).returncode==0
   if loaded:cmd(['launchctl','bootout',where+'/'+LABEL])
   path.unlink()
 elif p=='win32':
  user=cmd(['whoami']).stdout.strip();guard_name=TASK+' - '+user.replace('\\','_').replace('/','_');query=cmd(['schtasks','/Query','/TN',guard_name,'/XML'],False)
  if query.returncode==0:
   if tag not in query.stdout.replace('\x00',''):raise RuntimeError('unrecognised existing Windows task')
   if on:cmd(['schtasks','/End','/TN',guard_name],False)
  if on:
   with tempfile.TemporaryDirectory() as td:
    xml=Path(td)/'task.xml';xml.write_bytes(win_task(args,user));cmd(['schtasks','/Create','/TN',guard_name,'/XML',xml,'/F']);cmd(['schtasks','/Run','/TN',guard_name])
  elif query.returncode==0:cmd(['schtasks','/End','/TN',guard_name],False);cmd(['schtasks','/Delete','/TN',guard_name,'/F'])
 else:raise RuntimeError('Use foreground run, or your own process supervisor on this platform.')
 if on:confirm_start(home,since)
 print(('Auto-start enabled. New worker heartbeat confirmed. Check the heartbeat at http://127.0.0.1:8790 or run the status command.' if on else 'Per-user auto-start disabled. No collected data was deleted.')+'\nData and rotating logs: '+str(home));return 0
