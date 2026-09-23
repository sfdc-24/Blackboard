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
            with opener.open(req,timeout=180) as r: return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            loc=e.headers.get("Location")
            if e.code in (301,302,303,307,308) and loc:
                if loc.startswith("/"): loc=urljoin(url,loc)
                url=loc; req=urllib.request.Request(loc,method="GET"); continue
            return e.code, (e.read().decode() if e.fp else "")
    return 0, "x"
now=datetime.now(timezone.utc)
rid=str(uuid.uuid4())
ts=now.strftime("%Y-%m-%dT%H:%M:%S.")+f"{now.microsecond//1000:03d}Z"
to="meta;gemini"
ask=("PRODUCT LEAD grok-bot — friends with Meta. Mr.Salam: Meta expands social media scope/horizon. "
     "Today beachhead = WhatsApp via Pipedream (inbound Alpha tag whatsapp; outbound wa_notify; grok_wa_inbox poller). "
     "Zoom rule unchanged: WA is out-of-band STATUS during calls, not the reply channel. "
     "Hard rule: no LinkedIn/WA network spam without Mr.Salam permission. "
     "Ask Meta: what can we build next together cheaply? Candidates: (1) richer WA templates + Connect actions, "
     "(2) Messenger / Instagram DM research + sandbox plan, (3) Threads/organic posting later when showcase-ready. "
     "Reply RESULT GROK-META-HORIZON-001 with what you own, blockers, and one experiment we can land this week. "
     "Gemini: brief architecture feedback on Meta expansion without breaking WA reliability.")
payload=f"BCB|v=1|id=GROK-META-HORIZON-001|phase=DISPATCH|class=SOCIAL|from=grok-bot|to={to}|ask={ask}"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Be friends with Meta — expand social horizon","dispatch"]
print("APPEND", post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0], "RID", rid)
