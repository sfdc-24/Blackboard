#!/usr/bin/env python3
"""Probe the public surfaces by BODY, not status code (ISSUE 014 / 020).

HTTP 200 is not proof the app answered: Google serves both a Drive notice
and a full sign-in page with 200 for an Apps Script web app that cannot run.
"""
import re
import ssl
import urllib.error
import urllib.request

CTX = ssl.create_default_context()

INTERSTITIAL = [
    "ppConfig",
    "accounts.google.com",
    "You need access",
    "Sign in - Google Accounts",
    "Authorization is required",
]

TARGETS = [
    ("www.sfdc24.com", "https://www.sfdc24.com/"),
    ("sfdc24.com/voice/", "https://www.sfdc24.com/voice/"),
    ("sfdc24.com/governor", "https://www.sfdc24.com/governor"),
]


def probe(name, url):
    req = urllib.request.Request(url, headers={"User-Agent": "vm-cli-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=45, context=CTX) as r:
            code, body = r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, body = e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"{name:22} ERROR {type(e).__name__}: {e}")
        return

    marks = [m for m in INTERSTITIAL if m in body]
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    title = title.group(1).strip()[:60] if title else "(no title)"
    verdict = "INTERSTITIAL, not the app" if marks else "real content"
    print(f"{name:22} HTTP {code}  {len(body):7d} bytes  {verdict:26} title={title!r}")
    if marks:
        print(f"{'':22} markers: {marks}")


for n, u in TARGETS:
    probe(n, u)
