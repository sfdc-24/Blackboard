#!/usr/bin/env python3
"""Pull the dev org into a JSON snapshot the public site can render.

WHY A SNAPSHOT AND NOT A LIVE CALL
  sfdc24.com is a static GitHub Pages site. Measured 2026-09-17: a POST to it
  returns 405, because there is no server there at all. So the page cannot hold
  a credential, cannot do OAuth, and cannot proxy a query. Anything "live" on
  that page would need a server we do not have yet.

  This runs where the credential already is, writes one JSON file, and the site
  serves it as a static asset. No secret leaves this machine, and the page needs
  no new infrastructure.

WHAT THIS IS AND IS NOT
  The org is `dbm00000wk2ibeac-dev-ed.develop.my.salesforce.com` — OUR OWN
  developer edition org carrying Salesforce's sample data. It is NOT a client
  org and never should be. If the domain in .env ever points at a customer
  tenant, this script must not be run: it writes to a PUBLIC file.

  Every record here becomes world-readable. So the field list is deliberately
  narrow: names, stages, amounts, dates, industry. NO Email, NO Phone, NO
  MailingAddress, NO owner identities. The sample data would make those
  harmless, which is exactly why refusing them now matters — the habit is what
  survives, not the dataset.

THE CLAIM THIS CHANGES, WHICH IS THE REAL COST
  The homepage's #honest-boundary currently states: "What does not exist yet is
  the part that reads a live org" and "The connector is designed and not built."
  Shipping this makes both FALSE. The denial text is pinned in three places
  (the page, tests/site_positioning.cjs, tests/honesty.spec.cjs) and all three
  have to change in the same commit as the first published snapshot — otherwise
  the page either fails its own gate or, far worse, passes while lying.

  The honest replacement is narrow and true: this reads OUR development org,
  not a customer's, and nothing here has scored anyone's live system.

D-18: credentials come from .env at run time. Never argv, never logged, never
written into the output file.

USAGE
  python scripts/org_snapshot.py --check
  python scripts/org_snapshot.py --out ../site-main/data/org.json
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = os.path.join(REPO, ".env")
API = "v62.0"

# A DE / sandbox host only. A production-looking host is a refusal, not a warning.
ALLOWED_HOST_MARKERS = ("develop.my.salesforce.com", "sandbox.my.salesforce.com",
                        "scratch.my.salesforce.com")


def load_env():
    kv = {}
    if os.path.exists(ENV):
        with open(ENV, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=\s*(.+?)\s*$", line)
                if m:
                    kv[m.group(1)] = m.group(2)
    return kv


def creds():
    kv = load_env()
    dom = (kv.get("Headless_domain") or "").strip()
    cid = kv.get("Headless_consumer_key")
    sec = kv.get("Headless_consumer_secret")
    if not dom or not cid or not sec:
        return None, None, None, "Headless_domain / _consumer_key / _consumer_secret not all present in .env"
    if not dom.startswith("http"):
        dom = "https://" + dom
    dom = dom.rstrip("/")
    host = urllib.parse.urlparse(dom).netloc
    if not any(mark in host for mark in ALLOWED_HOST_MARKERS):
        # Refuse rather than warn. This writes a world-readable file.
        return None, None, None, (
            "REFUSING: %s does not look like a developer or sandbox org. This "
            "script publishes to a public file and must never be pointed at a "
            "customer tenant." % host)
    return dom, cid, sec, None


def token(dom, cid, sec, attempts=4):
    """Get a client-credentials token, retrying a transient failure.

    WHY THE RETRY. On 2026-09-17 this call returned HTTP 404 once, minutes after
    succeeding twice against the same host with the same credentials, and then
    succeeded three more times immediately afterwards through three different
    clients. A raw POST with redirects disabled answered 200 with no Location
    header, so it was neither urllib's redirect handling nor an org-side change
    — the two causes worth suspecting. The cause is UNCONFIRMED; what is certain
    is that a single blip killed the entire run, because there was no retry.

    The board gateway on this same fleet needed three attempts that same hour to
    get past a stub response. Two flaky endpoints in one evening is enough to
    treat one-shot network calls as the defect rather than the norm.

    A 400/401 is NOT retried: bad credentials do not improve with repetition,
    and retrying them looks like a brute-force attempt from the org's side.
    """
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid, "client_secret": sec
    }).encode()
    last = None
    for i in range(1, attempts + 1):
        req = urllib.request.Request(dom + "/services/oauth2/token", data=body, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if i > 1:
                    print("  token   : OK on attempt %d" % i)
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code in (400, 401, 403):
                # Credential or permission problem. Say so once and stop.
                raise RuntimeError("%s — credentials or scopes, not transient: %s"
                                   % (last, e.read().decode("utf-8", "replace")[:200]))
            print("  token   : attempt %d got %s, retrying" % (i, last))
        except Exception as exc:
            last = repr(exc)
            print("  token   : attempt %d failed (%s), retrying" % (i, last))
        time.sleep(1.5 * i)
    raise RuntimeError("token failed after %d attempts; last was %s" % (attempts, last))


def query(inst, tok, soql, attempts=3):
    """Run one SOQL query, retrying a transient failure for the same reason
    token() does. A 400 (malformed SOQL) or 401/403 (scope) is returned
    immediately — those are answers about the request, not weather."""
    url = "%s/services/data/%s/query?q=%s" % (inst, API, urllib.parse.quote(soql))
    last = None
    for i in range(1, attempts + 1):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace")), None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if e.code in (400, 401, 403):
                return None, "HTTP %s: %s" % (e.code, detail)
            last = "HTTP %s: %s" % (e.code, detail)
        except Exception as exc:
            last = str(exc)
        if i < attempts:
            time.sleep(1.5 * i)
    return None, "%s (after %d attempts)" % (last, attempts)


# Narrow ON PURPOSE. Everything selected here becomes world-readable.
SOQL = {
    "accounts": ("SELECT Name, Industry, Type, AnnualRevenue, NumberOfEmployees, "
                 "BillingState, BillingCountry FROM Account ORDER BY AnnualRevenue DESC NULLS LAST LIMIT 40"),
    "contacts": ("SELECT Name, Title, Department, Account.Name FROM Contact "
                 "ORDER BY Name LIMIT 40"),
    "opportunities": ("SELECT Name, StageName, Amount, CloseDate, Probability, "
                      "IsClosed, IsWon, Type, Account.Name FROM Opportunity "
                      "ORDER BY Amount DESC NULLS LAST LIMIT 60"),
}


def flatten(rec):
    """Drop attributes and lift Account.Name, so the page needs no unwrapping."""
    out = {}
    for k, v in rec.items():
        if k == "attributes":
            continue
        if isinstance(v, dict):
            name = v.get("Name")
            if name is not None:
                out[k + "Name"] = name
            continue
        out[k] = v
    return out


def summarise(opps, accts, cons):
    won = [o for o in opps if o.get("IsWon")]
    open_ = [o for o in opps if not o.get("IsClosed")]
    lost = [o for o in opps if o.get("IsClosed") and not o.get("IsWon")]

    def total(rows):
        return round(sum(float(o.get("Amount") or 0) for o in rows), 2)

    by_stage = {}
    for o in opps:
        s = o.get("StageName") or "(none)"
        e = by_stage.setdefault(s, {"count": 0, "amount": 0.0})
        e["count"] += 1
        e["amount"] = round(e["amount"] + float(o.get("Amount") or 0), 2)

    by_industry = {}
    for a in accts:
        i = a.get("Industry") or "(none)"
        by_industry[i] = by_industry.get(i, 0) + 1

    return {
        "accounts": len(accts),
        "contacts": len(cons),
        "opportunities": len(opps),
        "pipeline_open": total(open_),
        "closed_won": total(won),
        "closed_lost": total(lost),
        "open_count": len(open_),
        "won_count": len(won),
        "lost_count": len(lost),
        "largest_open": max([float(o.get("Amount") or 0) for o in open_], default=0),
        "by_stage": by_stage,
        "by_industry": by_industry,
    }


def cmd(args):
    dom, cid, sec, err = creds()
    if err:
        print("  " + err)
        return 1
    host = urllib.parse.urlparse(dom).netloc
    print("  org host : %s" % host)
    try:
        t = token(dom, cid, sec)
    except Exception as exc:
        print("  TOKEN FAILED: %s" % exc)
        return 1
    print("  scope    : %s" % t.get("scope"))
    inst = t.get("instance_url") or dom
    tok = t["access_token"]

    data, counts = {}, {}
    for name, soql in SOQL.items():
        res, e = query(inst, tok, soql)
        if e:
            print("  %-14s FAILED %s" % (name, e))
            return 1
        rows = [flatten(r) for r in res.get("records", [])]
        data[name] = rows
        counts[name] = len(rows)
        print("  %-14s %d rows" % (name, len(rows)))

    out = {
        "_meta": {
            # The page MUST render `caveat` verbatim. A snapshot presented as
            # live is the single dishonesty the rest of this site exists to
            # avoid, and a timestamp alone does not say so out loud.
            "live": False,
            "snapshot": True,
            "caveat": ("A snapshot of our own Salesforce developer org, taken "
                       "at the time below. Not live, and not anyone's customer "
                       "data."),
            "org_host": host,
            "org_kind": "developer edition, our own, carrying sample data",
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "api": API,
            "counts": counts,
            "fields_withheld": ["Email", "Phone", "MailingAddress", "OwnerId"],
            "why_withheld": ("this file is world-readable on a public site, so "
                             "contact channels are never included even from "
                             "sample data"),
        },
        "summary": summarise(data["opportunities"], data["accounts"], data["contacts"]),
        "accounts": data["accounts"],
        "contacts": data["contacts"],
        "opportunities": data["opportunities"],
    }

    # Belt and braces: assert no withheld field slipped through a nested object.
    #
    # SCAN THE RECORDS ONLY, NEVER THE ENVELOPE. The first version serialised
    # the whole document and refused if "Email" / "Phone" / "MailingStreet"
    # appeared anywhere — and _meta.fields_withheld lists exactly those words,
    # so the guard tripped on its own documentation and could never write a
    # file. A safety check that always fires is indistinguishable from a broken
    # feature, and it fails in the direction that looks responsible, which is
    # how it survives review.
    records = json.dumps({k: out[k] for k in ("accounts", "contacts", "opportunities")})
    for bad in ("Email", "Phone", "MailingStreet", "MailingCity", "OwnerId"):
        if bad in records:
            print("  REFUSING TO WRITE: field %r reached the records" % bad)
            return 1
    # An '@' in record data almost always means an address got through.
    if "@" in records:
        print("  REFUSING TO WRITE: an '@' appears in the record data, which")
        print("  usually means an email address. Narrow the SOQL, do not widen this.")
        return 1

    s = json.dumps(out, indent=1, ensure_ascii=False)
    if args.check:
        print("\n  --- summary (not written; --check) ---")
        for k, v in out["summary"].items():
            if not isinstance(v, dict):
                print("    %-16s %s" % (k, v))
        print("\n  would write %d bytes" % len(s.encode("utf-8")))
        return 0

    path = os.path.abspath(args.out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(s + "\n")
    print("  wrote    : %s  (%d bytes)" % (path, os.path.getsize(path)))
    print("  REMEMBER : the first publish of this must change #honest-boundary")
    print("             and the pinned denials in site_positioning.cjs and")
    print("             honesty.spec.cjs in the SAME commit.")
    return 0


def main():
    p = argparse.ArgumentParser(description="snapshot the dev org into public JSON")
    p.add_argument("--out", default=os.path.join(REPO, "..", "site-main", "data", "org.json"))
    p.add_argument("--check", action="store_true", help="query and summarise, write nothing")
    sys.exit(cmd(p.parse_args()))


if __name__ == "__main__":
    main()
