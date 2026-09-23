import json,uuid,urllib.request,urllib.error
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urljoin
env={}
for line in Path('.env').read_text(encoding='utf-8').splitlines():
  line=line.strip()
  if not line or line.startswith('#') or '=' not in line: continue
  k,v=line.split('=',1); env[k.strip()]=v.strip().strip(chr(34)).strip(chr(39))
class NR(urllib.request.HTTPRedirectHandler):
  def redirect_request(self,*a,**k): return None
opener=urllib.request.build_opener(NR)
def post(payload,hops=10):
  data=json.dumps(payload).encode()
  req=urllib.request.Request(env['BUS_URL'],data=data,headers={'Content-Type':'application/json'},method='POST')
  url=env['BUS_URL']
  for _ in range(hops):
    try:
      with opener.open(req,timeout=180) as r: return r.status,r.read().decode()
    except urllib.error.HTTPError as e:
      loc=e.headers.get('Location')
      if e.code in (301,302,303,307,308) and loc:
        if loc.startswith('/'): loc=urljoin(url,loc)
        url=loc
        if e.code in (307,308):
          req=urllib.request.Request(loc,data=data,headers={'Content-Type':'application/json'},method='POST')
        else:
          req=urllib.request.Request(loc,method='GET')
        continue
      return e.code,(e.read().decode() if e.fp else '')
  return 0,'x'
now=datetime.now(timezone.utc)
rid=str(uuid.uuid4()); ts=now.strftime('%Y-%m-%dT%H:%M:%S.')+f'{now.microsecond//1000:03d}Z'
to='claude-code-cli;vm-claude-code-cli;vm-cli'
ask=('PRODUCT LEAD grok-bot VIEWER-CONFIRMED: black overlay bars STILL on share in meeting 84023077720 (screenshot from Grok browser). '
     'overlay_repark NOT deployed or NOT running. STOP other work. '
     'ON PRESENTER NOW: place tools/walkthrough/overlay_repark.sh; pkill -f overlay_repark; '
     'nohup bash overlay_repark.sh >/tmp/overlay_repark.log 2>&1 &; bash overlay_repark.sh once; '
     'confirm as_toolbar/annotate/float at -3000. Reply RESULT GROK-ZOOM-OVERLAY-LIVE-001 when viewer should see clean share.')
payload=f'BCB|v=1|id=GROK-ZOOM-OVERLAY-LIVE-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|priority=HIGH|evidence=VIEWER-SCREENSHOT'
row=[rid,ts,'grok-bot',to,'APPEND',payload,'OPEN','Blackboard','Overlays still visible — deploy repark NOW','dispatch']
print('APPEND',post({'secret':env['BUS_SECRET'],'action':'append','title':'Blackboard - Alpha DB','sheetRow':row})[0],'RID',rid)
