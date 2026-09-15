#!/usr/bin/env python3
"""Seed a demo tenant on a local blackboard-bus with a mid-flight project story.
Usage: BUS_URL=http://127.0.0.1:8899/ BUS_SECRET=... python3 seed_demo_tenant.py
The story: a client project moving DISPATCH -> CLAIM -> PROGRESS -> FINDING -> RESULT,
so a prospect sees coordination happening, not an empty sheet."""
import json, os, sys, time, urllib.request

URL = os.environ["BUS_URL"].rstrip("/") + "/"
SECRET = os.environ["BUS_SECRET"]
TITLE = "Demo Board - Acme Logistics"
HEADER = ["Row_ID","Timestamp","Source_Tag","Target_Surface","Action_Type",
          "Payload","Category","Project Tag","Gist","Sub-Gist"]

def call(body):
    body = dict(body, secret=SECRET)
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def row(i, tag, action, payload, gist):
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    return [f"DEMO-{i:03d}", ts, tag, "ALL", action, payload, "OPEN",
            "Acme Intake", gist, ""]

def main():
    try:
        call({"action":"create","title":TITLE,"kind":"sheet","header":HEADER})
    except urllib.error.HTTPError as e:
        if e.code != 409: raise
    story = [
        row(1,"governor","DISPATCH","phase=DISPATCH|id=ACME-001|from=governor|to=intake-agent|task=map Acme's inbound-receipt process from the discovery call notes","kickoff"),
        row(2,"intake-agent","CLAIM","phase=CLAIM|id=ACME-001|by=intake-agent|lease=2h","claimed"),
        row(3,"intake-agent","PROGRESS","phase=PROGRESS|id=ACME-001|by=intake-agent|state=notes parsed, 14 process steps identified, 3 ambiguous","working"),
        row(4,"review-agent","FINDING","phase=FINDING|id=ACME-001|by=review-agent|finding=steps 7 and 9 conflict: receiving dock count vs ERP count reconciled at different times|severity=P2","caught early"),
        row(5,"intake-agent","RESULT","phase=RESULT|id=ACME-001|from=intake-agent|state=process map v1 delivered, conflict flagged for client confirmation|evidence=14 steps, 1 open question","delivered"),
        row(6,"governor","DISPATCH","phase=DISPATCH|id=ACME-002|from=governor|to=build-agent|task=draft the Salesforce object model for the confirmed 13 steps","next beat"),
    ]
    for r in story:
        out = call({"action":"append","title":TITLE,"sheetRow":r})
        print("appended", r[0], "->", out.get("rows", out))
    rd = call({"action":"read","title":TITLE})
    n = len(rd.get("rows", [])) - 1
    print(f"READ-BACK: {n} rows on '{TITLE}'")
    sys.exit(0 if n == len(story) else 1)

if __name__ == "__main__":
    main()
