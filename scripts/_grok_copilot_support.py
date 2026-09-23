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
to="ALL"
ask=("PRODUCT LEAD grok-bot: GitHub Copilot is now eng support for Blackboard + sfdc24.com under grok product direction (Mr.Salam confirmed). "
     "Route: Copilot=GitHub PRs/impl/docs/CI; Claude=SF/heavy; Codex=PM/evidence; Meta=WA; Gemini=second opinion; vm=ops/presenter; grok=product/orchestration. "
     "First Copilot tickets overnight: (1) PR from Blackboard grok/overnight-2026-09-18 (2) PR from sfdc24-site grok/cool-option-2026-09-18 additive cool/ only (3) land hear_in_zoom default Seconds 15->5 on Blackboard tip. "
     "Zoom peer rehearsal still OPEN — presenter SSH timeout; vm bring-up before human.")
payload=f"BCB|v=1|id=GROK-COPILOT-SUPPORT-001|phase=RESULT|class=FLEET|from=grok-bot|to={to}|ask={ask}|evidence=TESTED-user-confirmed"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","GitHub Copilot supports grok product lead","status"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0], "RID", rid)
