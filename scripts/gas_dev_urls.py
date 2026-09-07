#!/usr/bin/env python3
"""Print full deployment ids and web-app URLs for a script. READ ONLY."""
import json
import os
import sys
import urllib.parse
import urllib.request


def token():
    d = json.load(open(os.path.expanduser("~/.clasprc.json")))["tokens"]["default"]
    body = urllib.parse.urlencode({
        "client_id": d["client_id"], "client_secret": d["client_secret"],
        "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]


tok = token()
for script_id in sys.argv[1:]:
    url = f"https://script.googleapis.com/v1/projects/{script_id}/deployments"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = json.load(r)
    print(f"\n=== {script_id}")
    for d in data.get("deployments", []):
        dc = d.get("deploymentConfig", {})
        ver = dc.get("versionNumber", "HEAD")
        did = d.get("deploymentId", "")
        print(f"  version={ver}  id={did}")
        print(f"    desc={dc.get('description','')}")
        for ep in d.get("entryPoints", []):
            wa = ep.get("webApp")
            if wa:
                print(f"    exec={wa.get('url')}")
                print(f"    dev =https://script.google.com/macros/s/{did}/dev")
