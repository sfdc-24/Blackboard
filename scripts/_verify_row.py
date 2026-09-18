import json, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urljoin
REPO = Path(r"C:\users\salam\quantum\blackboard")
env = {}
for line in (REPO / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line: continue
    k, v = line.split("=", 1); env[k.strip()] = v.strip().strip('"').strip("'")
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k): return None
def bus(payload, hops=8):
    opener = urllib.request.build_opener(NoRedirect)
    url = env["BUS_URL"]
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    for _ in range(hops):
        try:
            with opener.open(req, timeout=180) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location"); body = e.read().decode("utf-8","replace") if e.fp else ""
            if e.code in (301,302,303,307,308) and loc:
                url = urljoin(url, loc) if loc.startswith("/") else loc
                req = urllib.request.Request(url, method="GET"); continue
            return e.code, body
    return 0, "too many redirects"
for attempt in (1,2,3,4):
    code, body = bus({"secret": env["BUS_SECRET"], "action":"read", "title":"Blackboard - Alpha DB"})
    if not body.lstrip().startswith("{"):
        print("attempt %d: HTTP %s, non-JSON page (%d bytes) - unknown, retrying" % (attempt, code, len(body)))
    else:
        data = json.loads(body)
        if "rows" not in data:
            print("attempt %d: HTTP %s, health reply %s - unknown, retrying" % (attempt, code, sorted(data.keys())))
        else:
            rows = [r for r in (data.get("rows") or []) if isinstance(r, list)]
            hits = [r for r in rows if "CCC-GROK-PRESENTER-001" in str(r[5] if len(r)>5 else "")]
            print("attempt %d: HTTP %s, %d rows" % (attempt, code, len(rows)))
            print("  CCC-GROK-PRESENTER-001 present: %d time(s)" % len(hits))
            for h in hits:
                print("    %s  from=%s" % (str(h[1])[:19], h[2]))
            mine = [r for r in rows if len(r)>2 and str(r[2]).strip()=="claude-code-cli" and str(r[1]).startswith("2026-09-18")]
            print("  rows written by claude-code-cli today: %d" % len(mine))
            break
    if attempt < 4:
        time.sleep(15)
