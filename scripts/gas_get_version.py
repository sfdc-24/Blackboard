"""Fetch the source of a SPECIFIC deployed Apps Script version.

This is the read-back that did not exist on 2026-09-03, when a security fix
was 'deployed' six times and the one deploy that completed silently
republished Version 12. Source at HEAD is not evidence about a pinned
deployment; this reads the pinned version itself.
"""
import json, os, sys, urllib.request, urllib.parse

def token():
    d = json.load(open(os.path.expanduser("~/.clasprc.json")))["tokens"]["default"]
    body = urllib.parse.urlencode({
        "client_id": d["client_id"], "client_secret": d["client_secret"],
        "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["access_token"]

def content(script_id, version, tok):
    url = f"https://script.googleapis.com/v1/projects/{script_id}/content?versionNumber={version}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.load(r)

script_id, version, outdir = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(outdir, exist_ok=True)
data = content(script_id, version, token())
for f in data.get("files", []):
    ext = {"SERVER_JS": "gs", "HTML": "html", "JSON": "json"}.get(f["type"], "txt")
    path = os.path.join(outdir, f"{f['name']}.{ext}")
    open(path, "w", encoding="utf-8", newline="\n").write(f["source"])
    print(f"{len(f['source']):>7} B  {f['name']}.{ext}")
