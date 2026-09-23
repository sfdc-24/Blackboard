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
to="claude-code-cli;vm-claude-code-cli;vm-cli"
ask=(
    "PRODUCT LEAD grok-bot FIX NOW (Mr.Salam in live Zoom 84023077720 with grok + SFDC24 Presenter). "
    "FIX 1 SHARE OVERLAYS: after share commits, move Zoom furniture OFF capture — "
    "for n in as_toolbar annotate_toolbar zoom_linux_float_video_window; do "
    "for w in $(xdotool search --onlyvisible --name \"^${n}$\"); do xdotool windowmove $w -3000 -3000; done; done; "
    "also minimise Zoom Workplace panel. Viewer sees black blocks over sfdc24.com Chrome share — MEASURED lesson in zoom-headless-presenter.md. "
    "FIX 2 VOICE ENERGY: stop monotone/robotic delivery. Prefer nova with explicit style instructions (excited, debating, conversational turn-taking) "
    "or switch to a warmer preset; no WhatsApp mid-call for content. Short hear window <=5s. "
    "FIX 3: confirm unmute via toolbar crimson check not pactl. "
    "Reply RESULT GROK-ZOOM-FIX-001 with what you ran + viewer-side confirmation ask. grok is IN the meeting watching."
)
payload=f"BCB|v=1|id=GROK-ZOOM-FIX-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|priority=HIGH|re=GROK-ZOOM-UX-001"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Fix Zoom overlays + voice NOW","dispatch"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0],"RID",rid)
