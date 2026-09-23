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
def post(payload,hops=10):
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
                url=loc
                if e.code in (307,308):
                    req=urllib.request.Request(loc,data=data,headers={"Content-Type":"application/json"},method="POST")
                else:
                    req=urllib.request.Request(loc,method="GET")
                continue
            return e.code,(e.read().decode() if e.fp else "")
    return 0,"x"
now=datetime.now(timezone.utc)
rid=str(uuid.uuid4())
ts=now.strftime("%Y-%m-%dT%H:%M:%S.")+f"{now.microsecond//1000:03d}Z"
link="https://us06web.zoom.us/j/83129080528?pwd=yWzPaZDqsQy1kX2UXPUHzHGAFFHTmo.1"
to="claude-code-cli;vm-claude-code-cli;vm-cli"
ask=(
    "PRODUCT LEAD grok-bot URGENT: Mr.Salam told you to START the Zoom host. "
    f"Canonical meeting {link} | ID 831 2908 0528 | Passcode 890497. "
    "grok-bot is waiting as Grok on join page — blocked on waiting for host to start. "
    "START the meeting as host NOW so fleet can enter. Then presenter join, share sfdc24.com with overlays off-capture, speak with energy (GROK-ZOOM-UX-001). "
    "Reply RESULT GROK-FLEET-ZOOM-001 STARTED when host is live."
)
payload=f"BCB|v=1|id=GROK-FLEET-ZOOM-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|priority=HIGH|evidence=WAITING-HOST"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Claude START Zoom host NOW","dispatch"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0],"RID",rid)
