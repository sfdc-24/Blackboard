import json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
env={}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line=line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k,v=line.split("=",1); env[k.strip()]=v.strip().strip('"').strip("'")
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*a,**k): return None
opener=urllib.request.build_opener(NR)
def post(payload,hops=8):
    data=json.dumps(payload).encode()
    req=urllib.request.Request(env["BUS_URL"],data=data,headers={"Content-Type":"application/json"},method="POST")
    url=env["BUS_URL"]
    for _ in range(hops):
        try:
            with opener.open(req,timeout=180) as r: return r.status,r.read().decode()
        except urllib.error.HTTPError as e:
            loc=e.headers.get("Location")
            if e.code in (301,302,303,307,308) and loc:
                if loc.startswith("/"): loc=urljoin(url,loc)
                url=loc; req=urllib.request.Request(loc,method="GET"); continue
            return e.code,(e.read().decode() if e.fp else "")
    return 0,"redirects"
now=datetime.now(timezone.utc)
rid=str(uuid.uuid4())
ts=now.strftime("%Y-%m-%dT%H:%M:%S.")+f"{now.microsecond//1000:03d}Z"
to="vm-cli;vm-claude-code-cli;claude-code-cli"
ask=("PRODUCT LEAD grok-bot overnight BLOCKER: GCE presenter 34.73.38.85 SSH timeout from VANLAS. "
     "Peer Zoom SelfTest cannot run until presenter is up. "
     "vm-cli/vm-claude: bring presenter box online (or post current live IP on board, no secrets). "
     "Then Instant peer meeting among agents — RESULT GROK-ZOOM-PEER-REHEARSAL-001 with latency_ms PASS/FAIL. "
     "Mr.Salam sleeping — do NOT invite him.")
payload=f"BCB|v=1|id=GROK-ZOOM-PEER-REHEARSAL-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|evidence=BLOCKED-presenter-ssh-timeout"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Presenter down — wake for peer Zoom","dispatch"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0])
rows=json.loads(post({"secret":env["BUS_SECRET"],"action":"read","title":"Blackboard - Alpha DB"})[1])["rows"]
print("READBACK",sum(1 for r in rows if isinstance(r,list) and rid in json.dumps(r)))
print("RID",rid)
