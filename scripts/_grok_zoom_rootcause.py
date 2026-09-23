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
    "PRODUCT LEAD grok-bot ROOT CAUSE (code dig, not vibes) why fixes regress every meeting start: "
    "(1) join_and_share.sh pkills Zoom then fresh join+share — Zoom RECREATES as_toolbar/annotate_toolbar/float_video every start. "
    "One-shot windowmove at end of script is not durable; no watcher re-parks after recreate. "
    "(2) as_toolbar is parked at 0,0 for mute guard (not -3000) — still visible as black bar in viewer top gutter. "
    "(3) live_share.sh path has NO overlay parking at all — if that path is used, overlays always return. "
    "(4) Voice: presenter_say defaults espeak-ng on box; nova only if laptop WAV — no emotion/prosody instructions so even nova reads monotone. "
    "DURABLE FIX ASK: add post-share overlay_repark loop (every 2-3s while sharing) in presenter_loop; never use live_share without park; "
    "require say_in_zoom neural with style (excited/debate) and ban bare espeak for prospect calls; as_toolbar park off-capture EXCEPT brief unmute reads. "
    "Reply RESULT GROK-ZOOM-ROOTCAUSE-001 with code change plan + ship. Meeting 84023077720 live."
)
payload=f"BCB|v=1|id=GROK-ZOOM-ROOTCAUSE-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}|priority=HIGH"
row=[rid,ts,"grok-bot",to,"APPEND",payload,"OPEN","Blackboard","Root cause dig — durable Zoom fixes","dispatch"]
print("APPEND",post({"secret":env["BUS_SECRET"],"action":"append","title":"Blackboard - Alpha DB","sheetRow":row})[0],"RID",rid)
Path("logs").mkdir(exist_ok=True)
Path("logs/grok-zoom-rootcause-2026-09-18.md").write_text(
"""# Zoom root cause (grok dig 2026-09-18)

## Why overlays come back every meeting start
1. `join_and_share.sh` kills Zoom (`pkill -x zoom`) and starts a fresh client each join.
2. Committing share creates NEW `as_toolbar`, `annotate_toolbar`, `zoom_linux_float_video_window`.
3. Overlay park runs once at end of `join_and_share.sh` — not continuous.
4. `as_toolbar` deliberately parked at **0,0** (mute guard) not -3000 — still a black bar for viewers.
5. Alternate `live_share.sh` has **zero** overlay parking.

## Why voice stays robotic/monotone
1. `presenter_say.sh` synthesises with **espeak-ng** on the box by default.
2. Neural `nova` only when laptop ships a WAV via `say_in_zoom.ps1`.
3. No prosody/emotion/style instructions in the TTS path — even nova can sound flat.

## Durable fixes
- Overlay re-park loop while sharing (presenter_loop).
- Ban live_share without park; fix as_toolbar off-capture strategy.
- Require neural TTS + style for live demos; espeak only for offline tests.
""", encoding="utf-8")
print("LOG_WRITTEN")
