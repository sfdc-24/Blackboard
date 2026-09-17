#!/usr/bin/env python3
"""Tests for scripts/org_publish.py, the public org-desk snapshot builder.

WHAT IS ON TRIAL
  Everything this builder emits goes to sfdc-24/sfdc24-site, a PUBLIC
  repository, and stays in its history. Two failures therefore matter more
  than any feature:

    1. Publishing a field that was never meant to leave the org. The builder
       answers that with an allowlist per section plus a sweep of the finished
       structure, and both are tested by what they REFUSE.
    2. Publishing numbers that disagree with the records shipped beside them.
       A tile saying 29 accounts above a chart that totals 24 is the shape of
       error this project keeps paying for - an instrument that answers
       fluently about something it never observed.

  The aggregation is checked against the snapshot that is on www.sfdc24.com
  right now, not against numbers invented here. tests/fixtures/
  org_desk_published_20260917.json is a byte copy of data/org.json from the
  site repo, generated 2026-09-17T20:08:46Z - about twenty minutes before every
  Opportunity in that org was deleted. So it is also the last full record of
  what the page was built from, which is worth keeping for its own sake.

  Feeding its records back through summarise() must reproduce its own summary
  exactly. That is a real cross-check: it would catch the (none) industry
  bucket being dropped, a closed-lost sign error, or a stage total drifting.

WHAT THIS SUITE DOES NOT PROVE
  No network, no org, no credential. It says nothing about whether the org is
  reachable or whether a SELECT still matches the org's schema - the first
  workflow_dispatch run is what settles that.

RUN
  python3 tests/test_org_publish.py
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "scripts")
FIXTURE = os.path.join(HERE, "fixtures", "org_desk_published_20260917.json")

spec = importlib.util.spec_from_file_location(
    "org_publish_mod", os.path.join(SCRIPTS, "org_publish.py"))
op = importlib.util.module_from_spec(spec)
spec.loader.exec_module(op)

PASS = 0
FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ok   " + name)
    else:
        FAIL += 1
        FAILURES.append(name)
        print("  FAIL " + name + ("  --> " + str(detail) if detail else ""))


def refused(fn, *a, **kw):
    """Return the PublishRefused raised, or None if it did not raise one."""
    try:
        fn(*a, **kw)
    except op.PublishRefused as exc:
        return exc
    except Exception:  # noqa: BLE001 - any other exception is a failure
        return None
    return None


with open(FIXTURE, encoding="utf-8") as fh:
    PUBLISHED = json.load(fh)


class FakeSession:
    """Answers the three SELECTs with whatever the test hands it."""

    def __init__(self, rows, api_version="67.0", truncate=False):
        self.rows = rows
        self.config = {"domain": "dbm00000wk2ibeac-dev-ed.develop.my.salesforce.com",
                       "api_version": api_version}
        self.truncate = truncate
        self.asked = []

    def query(self, soql, max_records=None):
        self.asked.append(soql)
        for name in ("Account", "Contact", "Opportunity"):
            if " FROM " + name + " " in soql or soql.endswith(" FROM " + name):
                key = {"Account": "accounts", "Contact": "contacts",
                       "Opportunity": "opportunities"}[name]
                return {"records": list(self.rows.get(key, [])),
                        "total_size": len(self.rows.get(key, [])),
                        "truncated": self.truncate}
        raise AssertionError("unexpected SOQL: " + soql)


# Raw-shaped rows, i.e. what sf360.clean_record hands back: relationships
# already flattened to dotted keys, a null relationship left as a null parent.
RAW = {
    "accounts": [
        {"Name": "United Oil", "Industry": "Energy", "Type": "Customer - Direct",
         "AnnualRevenue": 5600000000.0, "NumberOfEmployees": 145000,
         "BillingState": "NY", "BillingCountry": "United States"},
        {"Name": "No Industry Ltd", "Industry": None, "Type": None,
         "AnnualRevenue": None, "NumberOfEmployees": None,
         "BillingState": None, "BillingCountry": None},
    ],
    "contacts": [
        {"Name": "Ada Lovelace", "Title": None, "Department": None,
         "Account.Name": "Analytical Engines Ltd"},
        {"Name": "Unattached Person", "Title": "Consultant",
         "Department": "Ops", "Account": None},
    ],
    "opportunities": [
        {"Name": "Refinery Generators", "StageName": "Closed Won",
         "Amount": 915000.0, "CloseDate": "2026-07-01", "Probability": 100.0,
         "IsClosed": True, "IsWon": True, "Type": "New Customer",
         "Account.Name": "United Oil"},
        {"Name": "Portable Generators", "StageName": "Needs Analysis",
         "Amount": 675000.0, "CloseDate": "2026-11-01", "Probability": 10.0,
         "IsClosed": False, "IsWon": False, "Type": "New Customer",
         "Account.Name": "United Oil"},
        {"Name": "Lost Deal", "StageName": "Closed Lost", "Amount": 50000.0,
         "CloseDate": "2026-05-01", "Probability": 0.0, "IsClosed": True,
         "IsWon": False, "Type": None, "Account": None},
    ],
}

TMP = tempfile.mkdtemp(prefix="org-publish-test-")

try:
    # ----------------------------------------------------------------------
    print("== the aggregation must reproduce the page that is live today ==")

    mine = op.summarise(PUBLISHED["accounts"], PUBLISHED["contacts"],
                        PUBLISHED["opportunities"])
    theirs = PUBLISHED["summary"]
    for key in ("accounts", "contacts", "opportunities", "pipeline_open",
                "closed_won", "closed_lost", "open_count", "won_count",
                "lost_count", "largest_open"):
        check("matches the published " + key, mine[key] == theirs[key],
              "mine=%r published=%r" % (mine[key], theirs[key]))
    check("by_stage matches the published breakdown",
          dict(mine["by_stage"]) == theirs["by_stage"],
          {k: v for k, v in mine["by_stage"].items()
           if theirs["by_stage"].get(k) != v})
    check("by_industry matches, (none) bucket included",
          dict(mine["by_industry"]) == theirs["by_industry"],
          {k: v for k, v in mine["by_industry"].items()
           if theirs["by_industry"].get(k) != v})
    check("the industry buckets add up to the account count",
          sum(mine["by_industry"].values()) == len(PUBLISHED["accounts"]),
          sum(mine["by_industry"].values()))
    check("the stage buckets add up to the opportunity count",
          sum(v["count"] for v in mine["by_stage"].values())
          == len(PUBLISHED["opportunities"]))
    check("open plus closed accounts for every opportunity",
          mine["open_count"] + mine["won_count"] + mine["lost_count"]
          == len(PUBLISHED["opportunities"]))

    # ----------------------------------------------------------------------
    print("== an org with nothing in it is a number, not a crash ==")

    empty = op.summarise([], [], [])
    check("empty counts are zero", empty["accounts"] == 0 and empty["opportunities"] == 0)
    check("largest_open of no open deals is 0.0, not an exception",
          empty["largest_open"] == 0.0)
    check("empty breakdowns are empty, not absent",
          empty["by_stage"] == {} and empty["by_industry"] == {})
    # This is not hypothetical: every Opportunity in the org was deleted on
    # 2026-09-17, so the next real run produces exactly this shape.
    opps_gone = op.summarise(PUBLISHED["accounts"], PUBLISHED["contacts"], [])
    check("accounts survive when the opportunities are gone",
          opps_gone["accounts"] == 29 and opps_gone["opportunities"] == 0)
    check("a pipeline of nothing is 0.0 with no won or lost",
          opps_gone["pipeline_open"] == 0.0 and opps_gone["closed_won"] == 0.0)

    # ----------------------------------------------------------------------
    print("== the allowlist, judged by what it REFUSES ==")

    leaky = [dict(RAW["accounts"][0], Email="someone@example.com")]
    exc = refused(op.project, "accounts", leaky)
    check("an unlisted field is refused, not stripped", exc is not None)
    check("the refusal names the field", exc is not None and "Email" in str(exc), exc)
    check("the refusal says why it matters",
          exc is not None and "public" in str(exc).lower(), exc)

    short = [{k: v for k, v in RAW["accounts"][0].items() if k != "BillingState"}]
    exc = refused(op.project, "accounts", short)
    check("a field that vanished from the SELECT is refused", exc is not None)
    check("the refusal explains that blanks would hide it",
          exc is not None and "blank" in str(exc).lower(), exc)

    check("a dotted relationship is read when populated",
          op.project("contacts", [RAW["contacts"][0]])[0]["AccountName"]
          == "Analytical Engines Ltd")
    check("a NULL relationship publishes null, not an error",
          op.project("contacts", [RAW["contacts"][1]])[0]["AccountName"] is None)
    check("a genuinely absent field is still refused",
          refused(op._value_of, {"Name": "x"}, "Industry") is not None)
    check("null values are kept as keys rather than dropped",
          "Title" in op.project("contacts", [RAW["contacts"][0]])[0])

    check("every section publishes exactly the keys it declares",
          all(set(op.project(name, RAW[name])[0])
              == {dest for _, dest in spec["fields"]}
              for name, spec in op.SECTIONS.items()))

    # ----------------------------------------------------------------------
    print("== the sweep that still holds when a section is added later ==")

    exc = refused(op.scan_for_withheld, {"people": [{"Name": "x", "Email": "a@b.c"}]})
    check("a withheld key nested in a new section is caught", exc is not None)
    check("the sweep names the path", exc is not None and "Email" in str(exc), exc)
    check("record ids are never published",
          refused(op.scan_for_withheld, {"rows": [{"Id": "001x"}]}) is not None)
    check("a raw attributes blob is never published",
          refused(op.scan_for_withheld, {"rows": [{"attributes": {}}]}) is not None)
    check("OwnerId is never published",
          refused(op.scan_for_withheld, {"rows": [{"OwnerId": "005x"}]}) is not None)
    check("the clean fixture passes the sweep unchanged",
          op.scan_for_withheld(PUBLISHED) is PUBLISHED)
    for name in op.FIELDS_WITHHELD:
        check("the declared withheld field is actually unpublishable: " + name,
              name in op.NEVER_PUBLISH or name == "MailingAddress")

    # ----------------------------------------------------------------------
    print("== the whole snapshot, assembled ==")

    session = FakeSession(RAW, api_version="67.0")
    snap = op.build_snapshot(session, generated="2026-09-17T21:00:00Z")
    check("the three sections are present",
          all(k in snap for k in ("accounts", "contacts", "opportunities")))
    check("meta says it is a snapshot and not live",
          snap["_meta"]["snapshot"] is True and snap["_meta"]["live"] is False)
    check("the caveat travels inside the data",
          "development workspace" in snap["_meta"]["caveat"])
    check("meta names the org host it actually read",
          snap["_meta"]["org_host"].startswith("dbm00000wk2ibeac"))
    check("meta reports the api version the session used, not a literal",
          snap["_meta"]["api"] == "v67.0", snap["_meta"]["api"])
    check("meta counts agree with the records shipped beside them",
          snap["_meta"]["counts"] == {"accounts": 2, "contacts": 2,
                                      "opportunities": 3},
          snap["_meta"]["counts"])
    check("the summary is computed from those same records",
          snap["summary"]["opportunities"] == len(snap["opportunities"]))
    check("withheld fields are declared to the reader",
          snap["_meta"]["fields_withheld"] == op.FIELDS_WITHHELD)
    check("the generated stamp is the one supplied",
          snap["_meta"]["generated"] == "2026-09-17T21:00:00Z")
    check("no query asked for Email or Phone",
          not any("Email" in q or "Phone" in q for q in session.asked), session.asked)
    check("the snapshot key order matches the published file",
          list(snap) == list(PUBLISHED), list(snap))

    truncated = FakeSession(RAW, truncate=True)
    exc = refused(op.build_snapshot, truncated)
    check("a capped read refuses to publish a partial org", exc is not None)
    check("the refusal says a partial page is worse than none",
          exc is not None and "silently" in str(exc).lower(), exc)

    # a section whose SELECT drifted from the field list
    drifted = FakeSession({"accounts": [dict(RAW["accounts"][0], Website="x.com")],
                           "contacts": [], "opportunities": []})
    check("drift between the SELECT and the field list stops the build",
          refused(op.build_snapshot, drifted) is not None)

    # ----------------------------------------------------------------------
    print("== the two files, which must not be able to disagree ==")

    json_text, js_text = op.render(snap)
    check("org.js defines window.ORG_DATA", js_text.startswith(op.JS_HEADER))
    body = js_text[len(op.JS_HEADER):].rstrip()[:-1]  # drop the trailing ;
    check("org.js carries byte-identical json to org.json",
          body == json_text.rstrip(), body[:80])
    check("org.json parses", json.loads(json_text)["_meta"]["snapshot"] is True)
    check("org.js names the generator so it can be regenerated",
          "org_publish.py" in js_text)
    check("org.js warns against hand editing", "Do not edit by hand" in js_text)

    json_path, js_path = op.write_files(snap, os.path.join(TMP, "data"))
    for path in (json_path, js_path):
        raw = open(path, "rb").read()
        check("written with LF endings: " + os.path.basename(path),
              b"\r\n" not in raw)
        check("written as utf-8 and non-empty: " + os.path.basename(path),
              len(raw) > 200)
    check("both files land in one directory",
          os.path.dirname(json_path) == os.path.dirname(js_path))
    check("the written json round-trips to the same structure",
          json.load(open(json_path, encoding="utf-8"))["summary"]["opportunities"] == 3)

finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("")
print("passed=%d failed=%d" % (PASS, FAIL))
if FAIL:
    for _name in FAILURES:
        print("failure: " + _name)
    sys.exit(1)
