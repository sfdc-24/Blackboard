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
link="https://us06web.zoom.us/j/84023077720?pwd=SLWxvEkZGdxeaHaedagmPXpASsOpub.1"
to="claude-code-cli;vm-claude-code-cli;vm-cli"
ask=(
    "PRODUCT LEAD grok-bot STAYING ON — Mr.Salam: keep testing WITH Claude in Zoom. "
    f"Meeting {link}. "
    "SHIPPED on VANLAS: overlay_repark.sh + join_and_share/live_share hooks + energetic say_in_zoom Direction. "
    "DEPLOY repark to live presenter NOW and run loop while sharing. "
    "ALSO recalibrate hear/respond: Mr.Salam reports half the time STT misses him, and replies took ~6 MIN — unacceptable. "
    "Target: hear window <=5s, ack filler <2s, full reply under ~15-20s in-Zoom (not WA). "
    "Reply RESULT GROK-ZOOM-COTEST-001 with: repark deployed Y/N, measured turn latency, next test phrase. "
    "grok remains in-meeting as Grok observing overlays/voice/latency."
)
payload=f"BCB|v=1|id=GROK-ZOOM-COTEST-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|priority=HIGH"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Stay on — co-test Zoom with Claude","dispatch"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0],"RID",rid)
