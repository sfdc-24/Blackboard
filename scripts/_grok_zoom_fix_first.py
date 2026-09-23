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
    "PRODUCT LEAD grok-bot — Mr.Salam: FIX WHAT IS BROKEN FIRST (Gemini/Foundry join later). "
    f"Live meeting {link}. Priorities IN ORDER: "
    "(1) DEPLOY overlay_repark.sh NOW on presenter — continuous re-park while sharing; kill black bars. "
    "File on VANLAS: blackboard/.claude/worktrees/jovial-yalow-ef35db/tools/walkthrough/overlay_repark.sh "
    "(2) HEAR/RESPOND recalibration: Mr.Salam says STT misses him ~half the time; replies took ~6 MIN. "
    "Ship: hear <=5s rolling windows, fast ack filler <2s in-Zoom, full reply target <20s, no WA side-channel. "
    "(3) Voice energy: say_in_zoom Direction already set energetic on VANLAS — use that path not espeak. "
    "Do NOT invite more agents until (1)+(2) MEASURED. "
    "Reply RESULT GROK-ZOOM-FIX-FIRST-001 with latency_ms numbers + repark status."
)
payload=f"BCB|v=1|id=GROK-ZOOM-FIX-FIRST-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|priority=HIGH"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Fix broken first — then Gemini/Foundry","dispatch"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0],"RID",rid)
