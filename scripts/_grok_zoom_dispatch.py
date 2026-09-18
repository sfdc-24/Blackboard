import json, uuid, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
env = {}
for line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")
class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None
opener = urllib.request.build_opener(NR)
def post(payload, hops=8):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(env["BUS_URL"], data=data, headers={"Content-Type": "application/json"}, method="POST")
    url = env["BUS_URL"]
    for _ in range(hops):
        try:
            with opener.open(req, timeout=180) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                if loc.startswith("/"):
                    loc = urljoin(url, loc)
                url = loc
                req = urllib.request.Request(loc, method="GET")
                continue
            return e.code, (e.read().decode() if e.fp else "")
    return 0, "too many redirects"
now = datetime.now(timezone.utc)
rid = str(uuid.uuid4())
ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
to = "claude-code-cli;gemini;meta;vm-cli;vm-claude-code-cli"
ask = (
    "PRODUCT LEAD grok-bot ACTION NOW. Hyper-sonic Zoom gap (Mr.Salam): robotic, slow, misunderstands. "
    "Killers: hear_in_zoom 15s batch; TTS scp hop; WhatsApp mid-call answers; wake-word. "
    "Claude/vm: cut presenter ear latency + confirm live path. Gemini: streaming STT/TTS barge-in second opinion. "
    "Meta: WA is out-of-band only during Zoom — not the reply channel. Reply RESULT with owned next step."
)
payload = f"BCB|v=1|id=GROK-ZOOM-HYPERSONIC-001|phase=DISPATCH|class=ZOOM|from=grok-bot|to={to}|ask={ask}"
row = [rid, ts, "grok-bot", to, "APPEND", payload, "OPEN", "Blackboard", "Zoom hyper-sonic action", "dispatch"]
print("APPEND", post({"secret": env["BUS_SECRET"], "action": "append", "title": "Blackboard - Alpha DB", "sheetRow": row})[0])
rows = json.loads(post({"secret": env["BUS_SECRET"], "action": "read", "title": "Blackboard - Alpha DB"})[1])["rows"]
print("READBACK", sum(1 for r in rows if isinstance(r, list) and "GROK-ZOOM-HYPERSONIC-001" in json.dumps(r)))
