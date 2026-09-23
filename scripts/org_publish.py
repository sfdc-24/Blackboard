#!/usr/bin/env python3
"""Build the public org-desk snapshot from the live dev org, fail-closed.

WHAT THIS IS FOR
----------------
www.sfdc24.com/org/ renders `data/org.js`, which defines `window.ORG_DATA`.
That file is served from `sfdc-24/sfdc24-site`, a PUBLIC repository, so
everything this script emits is world-readable forever - git history included.
It therefore does not "strip sensitive fields". It publishes an ALLOWLIST and
refuses to emit anything else.

WHY AN ALLOWLIST AND NOT A DENY LIST
------------------------------------
The published snapshot already withholds Email, Phone, MailingAddress and
OwnerId. A deny list holds that line only for the fields someone remembered:
add `PersonEmail` to a SOQL string a month from now and a deny list publishes
it, silently, to the whole internet. So each section here names the exact keys
it may produce, and `_refuse_unexpected` raises if a record carries one key
more or one key fewer. Fewer matters too - a column that quietly disappears
turns into a page that renders blanks and says nothing about why.

`scripts/sf360.py` is the read rail underneath. Its datasets deliberately
select Email and Phone because agents and the local proxy legitimately need
them; this publisher does not reuse those queries for exactly that reason. It
issues its own narrow SELECTs.

WHAT IT DOES NOT DO
-------------------
It writes files. It does not commit, push, or deploy - that is the workflow's
job, and keeping it out of here is what lets the whole thing be tested offline
with no org, no credential and no network.

Usage
-----
    python scripts/org_publish.py --out-dir build/data
    python scripts/org_publish.py --out-dir build/data --print-summary
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("sf360_rail", os.path.join(_HERE, "sf360.py"))
sf360 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sf360)


class PublishRefused(RuntimeError):
    """The data does not match what this file is allowed to publish."""


# The caveat and the withholding note travel INSIDE the data, because the page
# renders them verbatim. A snapshot cannot be drawn by a page that forgot to
# say it is one. These strings match what is published today; changing them
# changes what every visitor reads.
CAVEAT = ("A snapshot of a development workspace of our own, taken at the time "
          "below. Not live, and not anyone's customer data.")
ORG_KIND = "developer edition, our own, carrying sample data"
FIELDS_WITHHELD = ["Email", "Phone", "MailingAddress", "OwnerId"]
WHY_WITHHELD = ("this file is world-readable on a public site, so contact "
                "channels are never included even from sample data")

# Names that must never appear as a key anywhere in the output, whatever
# section grows later. The per-section allowlists are the primary guard; this
# is the one that still holds when somebody adds a section and forgets one.
NEVER_PUBLISH = ("Id", "attributes", "Email", "Phone", "MobilePhone",
                 "HomePhone", "OtherPhone", "Fax", "MailingAddress",
                 "MailingStreet", "OwnerId", "Owner", "PersonEmail",
                 "PersonMobilePhone", "BillingStreet", "Website")

# section -> (SOQL, [(source key, published key), ...])
#
# Every published key is listed here and nowhere else. The source key may be a
# dotted relationship path, which sf360.clean_record flattens - except when the
# relationship itself is null, which _value_of handles explicitly.
SECTIONS = collections.OrderedDict((
    ("accounts", {
        "soql": ("SELECT Name, Industry, Type, AnnualRevenue, NumberOfEmployees, "
                 "BillingState, BillingCountry FROM Account ORDER BY Name"),
        "fields": [("Name", "Name"), ("Industry", "Industry"), ("Type", "Type"),
                   ("AnnualRevenue", "AnnualRevenue"),
                   ("NumberOfEmployees", "NumberOfEmployees"),
                   ("BillingState", "BillingState"),
                   ("BillingCountry", "BillingCountry")],
    }),
    ("contacts", {
        "soql": ("SELECT Name, Title, Department, Account.Name FROM Contact "
                 "ORDER BY Name"),
        "fields": [("Name", "Name"), ("Title", "Title"),
                   ("Department", "Department"), ("Account.Name", "AccountName")],
    }),
    ("opportunities", {
        "soql": ("SELECT Name, StageName, Amount, CloseDate, Probability, "
                 "IsClosed, IsWon, Type, Account.Name FROM Opportunity "
                 "ORDER BY CloseDate"),
        "fields": [("Name", "Name"), ("StageName", "StageName"),
                   ("Amount", "Amount"), ("CloseDate", "CloseDate"),
                   ("Probability", "Probability"), ("IsClosed", "IsClosed"),
                   ("IsWon", "IsWon"), ("Type", "Type"),
                   ("Account.Name", "AccountName")],
    }),
))

# An account with no Industry is still an account. The published snapshot
# buckets those under "(none)" so the industry counts add up to the account
# count; dropping them makes a chart that quietly disagrees with the tile above
# it, and nothing on the page would say which one is wrong.
NO_INDUSTRY = "(none)"

JS_HEADER = ("/* Generated by scripts/org_publish.py. Do not edit by hand:\n"
             "   regenerate it, so the data and its caveat stay together. */\n"
             "window.ORG_DATA = ")


def _value_of(record, source):
    """Read one source key, distinguishing a null parent from a missing field.

    `Account.Name` arrives flattened when the relationship is populated. When
    the relationship is NULL, sf360.clean_record leaves `Account: None` and no
    dotted key at all - a contact with no account, which is ordinary. Anything
    else missing is not ordinary: it means the SELECT and this table disagree,
    and publishing a column of nulls would hide that.
    """
    if source in record:
        return record[source]
    if "." in source:
        root = source.split(".", 1)[0]
        if root in record and record[root] is None:
            return None
    raise PublishRefused(
        "field %r is not in the query result. The SOQL and the published field "
        "list disagree, and a column of nulls would hide that." % (source,))


def _source_keys(fields):
    """Every key a raw record may carry: each source, plus a dotted source's
    root, because a null relationship arrives as the root alone."""
    allowed = set()
    for source, _ in fields:
        allowed.add(source)
        if "." in source:
            allowed.add(source.split(".", 1)[0])
    return allowed


def _present(record, source):
    if source in record:
        return True
    return "." in source and source.split(".", 1)[0] in record


def _refuse_unexpected(section, record, fields):
    """Judge the RAW record, not the projected row.

    The first version of this checked the row it had just built out of the
    declared fields, which is tautological: an unlisted field in the query
    result was silently dropped and the check passed. Dropping happens to be
    safe - the field never reaches the file - but it means a SELECT that has
    drifted away from this table sails through unnoticed, and the next edit to
    either side is the one that publishes something. The suite caught it; it
    said "refused" and the code said "dropped".
    """
    allowed = _source_keys(fields)
    extra = set(record) - allowed
    if extra:
        raise PublishRefused(
            "%s would carry unlisted field(s) %s out of the org. This file goes "
            "to a public repository; add the field to SECTIONS deliberately or "
            "drop it from the SELECT." % (section, ", ".join(sorted(extra))))
    missing = [source for source, _ in fields if not _present(record, source)]
    if missing:
        raise PublishRefused(
            "%s is missing published field(s) %s. A column that disappears "
            "renders as blanks and says nothing about why."
            % (section, ", ".join(sorted(missing))))


def scan_for_withheld(payload, path="ORG_DATA"):
    """Walk the finished structure and refuse any key that must never ship."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in NEVER_PUBLISH:
                raise PublishRefused(
                    "%s.%s is a field this snapshot never publishes" % (path, key))
            scan_for_withheld(value, path + "." + str(key))
    elif isinstance(payload, list):
        for index, item in enumerate(payload[:200]):
            scan_for_withheld(item, "%s[%d]" % (path, index))
    return payload


def project(section, records):
    """Map raw org records onto exactly the keys this section may publish."""
    spec = SECTIONS[section]
    out = []
    for record in records:
        _refuse_unexpected(section, record, spec["fields"])
        row = {}
        for source, dest in spec["fields"]:
            row[dest] = _value_of(record, source)
        # Two sources mapping to one published key would drop a column without
        # either check above noticing: the record is clean and the row is a
        # subset of the allowlist.
        if len(row) != len(spec["fields"]):
            raise PublishRefused(
                "%s declares %d fields but produced %d keys; two sources share "
                "a published name." % (section, len(spec["fields"]), len(row)))
        out.append(row)
    return out


def _amount(opportunity):
    return opportunity.get("Amount") or 0


def summarise(accounts, contacts, opportunities):
    """Header tiles and the two breakdowns the page draws.

    Computed from the record lists that ship in the same file, never carried
    over from a previous run, so the narration cannot describe data the reader
    is not looking at.
    """
    open_opps = [o for o in opportunities if not o["IsClosed"]]
    won = [o for o in opportunities if o["IsWon"]]
    lost = [o for o in opportunities if o["IsClosed"] and not o["IsWon"]]

    by_stage = collections.OrderedDict()
    for opportunity in opportunities:
        entry = by_stage.setdefault(opportunity["StageName"],
                                    {"count": 0, "amount": 0.0})
        entry["count"] += 1
        entry["amount"] += _amount(opportunity)

    by_industry = collections.OrderedDict()
    for account in accounts:
        key = account.get("Industry") or NO_INDUSTRY
        by_industry[key] = by_industry.get(key, 0) + 1

    return collections.OrderedDict((
        ("accounts", len(accounts)),
        ("contacts", len(contacts)),
        ("opportunities", len(opportunities)),
        ("pipeline_open", float(sum(_amount(o) for o in open_opps))),
        ("closed_won", float(sum(_amount(o) for o in won))),
        ("closed_lost", float(sum(_amount(o) for o in lost))),
        ("open_count", len(open_opps)),
        ("won_count", len(won)),
        ("lost_count", len(lost)),
        ("largest_open", float(max([_amount(o) for o in open_opps] or [0]))),
        ("by_stage", by_stage),
        ("by_industry", by_industry),
    ))


def build_snapshot(session, generated=None, max_records=sf360.DEFAULT_MAX_RECORDS):
    """Pull every section and assemble the published structure."""
    sections = collections.OrderedDict()
    for name, spec in SECTIONS.items():
        result = session.query(spec["soql"], max_records=max_records)
        if result.get("truncated"):
            raise PublishRefused(
                "%s hit the %d-record cap. A page that silently shows part of "
                "the org is worse than one that fails to build."
                % (name, max_records))
        sections[name] = project(name, result["records"])

    config = getattr(session, "config", {}) or {}
    meta = collections.OrderedDict((
        ("live", False),
        ("snapshot", True),
        ("caveat", CAVEAT),
        ("org_host", config.get("domain", "")),
        ("org_kind", ORG_KIND),
        ("generated", generated or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        # The version actually used for this pull, read from the session rather
        # than written here: a snapshot must not claim an API it did not call.
        ("api", "v" + str(config.get("api_version", sf360.DEFAULT_API_VERSION))),
        ("counts", collections.OrderedDict(
            (name, len(rows)) for name, rows in sections.items())),
        ("fields_withheld", list(FIELDS_WITHHELD)),
        ("why_withheld", WHY_WITHHELD),
    ))

    payload = collections.OrderedDict((("_meta", meta),))
    payload["summary"] = summarise(sections["accounts"], sections["contacts"],
                                   sections["opportunities"])
    payload.update(sections)
    return scan_for_withheld(payload)


def render(payload):
    """Both files, from one structure, so they cannot disagree.

    org.json and org.js are emitted together because the page loads the .js by
    script element - fetch of a same-directory file is CORS-blocked under
    file://, which would make the page work on Pages and be unverifiable
    locally. Committing only one of the pair ships a stale page.
    """
    body = json.dumps(payload, indent=1)
    return body + "\n", JS_HEADER + body + ";\n"


def write_files(payload, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    json_text, js_text = render(payload)
    json_path = os.path.join(out_dir, "org.json")
    js_path = os.path.join(out_dir, "org.js")
    for path, text in ((json_path, json_text), (js_path, js_text)):
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    return json_path, js_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="org_publish",
        description="Build the public org-desk snapshot from the live dev org.")
    parser.add_argument("--out-dir", default="build/data",
                        help="directory to write org.json and org.js into")
    parser.add_argument("--max-records", type=int, default=sf360.DEFAULT_MAX_RECORDS)
    parser.add_argument("--print-summary", action="store_true",
                        help="print the header tiles to stdout")
    args = parser.parse_args(argv)

    session = sf360.SalesforceSession()
    identity = session.identity()
    org = str(identity.get("organization_id") or "")
    if not org.startswith(sf360.EXPECTED_ORG_PREFIX):
        sys.stderr.write(
            "refusing to publish: credentials point at org %s, not %s\n"
            % (org or "<none>", sf360.EXPECTED_ORG_PREFIX))
        return 2

    try:
        payload = build_snapshot(session, max_records=args.max_records)
    except PublishRefused as exc:
        sys.stderr.write("REFUSED TO PUBLISH: %s\n" % (exc,))
        return 3

    json_path, js_path = write_files(payload, args.out_dir)
    counts = payload["_meta"]["counts"]
    sys.stderr.write(
        "wrote %s and %s\n  org %s as %s, api %s\n  %s\n"
        % (json_path, js_path, org, identity.get("username"),
           payload["_meta"]["api"],
           ", ".join("%s %d" % (k, v) for k, v in counts.items())))
    if args.print_summary:
        sys.stdout.write(json.dumps(payload["summary"], indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
