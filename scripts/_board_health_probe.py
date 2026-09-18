import json, time, urllib.error, urllib.request
from pathlib import Path
from urllib.parse import urljoin

REPO = Path(r"C:\users\salam\quantum\blackboard")
env = {}
for line in (REPO / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    env[k.strip()] = v.strip().strip('"').strip("'")

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None

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
            loc = e.headers.get("Location")
            body = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code in (301,302,303,307,308) and loc:
                url = urljoin(url, loc) if loc.startswith("/") else loc
                req = urllib.request.Request(url, method="GET")
                continue
            return e.code, body
    return 0, "too many redirects"

for attempt in (1, 2, 3):
    code, body = bus({"secret": env["BUS_SECRET"], "action": "read", "title": "Blackboard - Alpha DB"})
    print("attempt %d: HTTP %s, %d bytes" % (attempt, code, len(body)))
    if body.lstrip().startswith("{"):
        data = json.loads(body)
        print("   top-level keys: %s" % sorted(data.keys()))
        for k in ("ok", "error", "message", "status"):
            if k in data:
                print("   %s = %r" % (k, data[k]))
        rows = data.get("rows")
        if rows is None:
            print("   'rows' key ABSENT -> degraded response, not an empty board")
        else:
            print("   rows present, count = %d" % len(rows))
            if rows:
                last = [r for r in rows if isinstance(r, list)][-1]
                print("   newest row: %s  from=%s" % (str(last[1])[:19], last[2]))
                break
    else:
        print("   body is not JSON, first 140: %s" % body[:140].replace("\n", " "))
    if attempt < 3:
        print("   pausing 20s before retry (throttling would clear, a wiped board would not)")
        time.sleep(20)
