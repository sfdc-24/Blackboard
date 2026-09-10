#!/usr/bin/env python3
"""xray-score-v1 - the SFDC24 org scoring engine.

WHY THIS EXISTS, AND WHY IT IS PYTHON
  /xray/ shipped with synthetic figures from a made-up tenant. This produces the
  real ones. It is stdlib-only Python rather than PowerShell on purpose: the
  fleet already carries ten `#Requires -Version 5.1` scripts that the GCloud
  migration has to port to Linux, and a new engine written in 5.1 would add to
  that debt on the very day we are trying to pay it down. This runs unchanged on
  the laptop and on an Ubuntu instance.

THE RULE THIS FILE EXISTS TO ENFORCE
  A check that cannot tell must SAY SO, and it must say WHICH KIND of "cannot".

  There are three answers, never two:

    SCORED          the population existed and was measured
    NOT_APPLICABLE  the population was genuinely empty - nothing COULD be a defect
    UNKNOWN         we could not find out - a query failed, the org was
                    unreachable, something threw

  Collapsing UNKNOWN into NOT_APPLICABLE is how a total outage reports as a
  clean org. Reproduced offline with every query failing: the first version of this
  file built zero checks, reported "no check had a
  population that could contain a defect", and exited 0 - indistinguishable from
  a genuinely fresh org. That is the same failure shape as counting "never
  logged in" as "stale": a confident answer where there was no evidence.

  The metric defect that started this, described by SHAPE rather than by the
  numbers it produced. The stale-user check counted "never logged in" as
  "stale". In an org where the active users had all been provisioned inside the
  window, every one of them scored as a defect - and the defect rate was
  computed over a population that could not contain a defect. The measurements
  that exposed it came from a scan taken under a hold, so they are not
  reproduced here and no fixture is derived from them.

UNITS ARE NOT INTERCHANGEABLE
  A dormant user and an inactive flow are not the same kind of thing, so their
  opportunities cannot be added. Pooling them produced one DPMO with no
  defensible denominator. Rollups are per-unit, and a cross-unit total is simply
  not offered.

USAGE
  python3 scripts/xray_score.py
  python3 scripts/xray_score.py --json out.json
  python3 scripts/xray_score.py --env /path/to/.env --window 90

  Credentials come from .env (D-18): Headless_domain, Headless_consumer_key,
  Headless_consumer_secret. Never from argv, never echoed.

EXIT CODES
  0  every check reached a verdict (SCORED or NOT_APPLICABLE)
  2  at least one check is UNKNOWN - the run did not establish what it claims

READ-ONLY BY CONSTRUCTION
  Every Data API call goes through get_json(), which is GET-only and refuses a
  host that is not the expected org. There is no write path and there must never
  be one - a scoring engine that can change the org it is scoring is a different
  and far more dangerous product.
"""
import argparse
import json
import math
import re
import sys
import urllib.parse
import urllib.request

API = "v67.0"

SCORED = "SCORED"
NOT_APPLICABLE = "NOT_APPLICABLE"
UNKNOWN = "UNKNOWN"


class Check:
    """One measurable thing, as opportunities and defects.

    `opportunities` is the population that COULD have been a defect; `defects`
    is how many of them were. Either may be None, which means we could not find
    out - and that is reported as UNKNOWN, never as zero.
    """

    def __init__(self, key, title, opportunities, defects, unit="record",
                 note="", reason=""):
        if opportunities is None or defects is None:
            self.state = UNKNOWN
        else:
            if defects < 0 or opportunities < 0:
                raise ValueError(key + ": counts cannot be negative")
            if defects > opportunities:
                raise ValueError(
                    "{0}: {1} defects out of {2} opportunities - more defects than "
                    "chances to have one means the query is wrong, not that the "
                    "org is broken".format(key, defects, opportunities))
            self.state = SCORED if opportunities > 0 else NOT_APPLICABLE
        self.key = key
        self.title = title
        self.opportunities = opportunities
        self.defects = defects
        self.unit = unit
        self.note = note
        self.reason = reason

    @property
    def scoreable(self):
        return self.state == SCORED

    @property
    def dpmo(self):
        if self.state != SCORED:
            return self.state
        return round(self.defects / self.opportunities * 1000000)

    @property
    def sigma(self):
        if self.state != SCORED:
            return self.state
        good = 1.0 - (self.defects / self.opportunities)
        if good >= 1.0:
            # A clean run is real, but "infinite sigma" is not a claim worth
            # making from a finite sample. Report the ceiling and say why.
            return {"value": None, "bounded": True,
                    "why": "no defects in {0} opportunities; sigma is unbounded "
                           "above, which is not a measurement".format(self.opportunities)}
        if good <= 0.0:
            return {"value": 0.0, "bounded": False,
                    "why": "every opportunity was a defect"}
        return {"value": round(inv_norm_cdf(good) + 1.5, 2), "bounded": False, "why": ""}

    def to_dict(self):
        return {"key": self.key, "title": self.title, "unit": self.unit,
                "state": self.state,
                "opportunities": self.opportunities, "defects": self.defects,
                "dpmo": self.dpmo, "sigma": self.sigma,
                "note": self.note, "reason": self.reason}


def inv_norm_cdf(p):
    """Acklam's inverse normal CDF.

    /xray/ states its method as sigma = inverse-normal(1 - DPMO/1e6) + 1.5, so
    that is implemented rather than approximated, and it is tested against known
    quantiles instead of being taken on faith.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be strictly between 0 and 1")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    lo, hi = 0.02425, 1 - 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return ((((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
                / (((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)))
    if p > hi:
        q = math.sqrt(-2 * math.log(1 - p))
        return -((((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5])
                 / (((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)))
    q = p - 0.5
    r = q * q
    return ((((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
            / ((((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)))


def summarise(checks):
    """Roll up PER UNIT, and never hide an UNKNOWN.

    Opportunities of different units are different kinds of thing. A dormant
    user and an inactive flow cannot be added, so no cross-unit total is offered
    - a single DPMO over both would have no defensible denominator.
    """
    unknown = [c for c in checks if c.state == UNKNOWN]
    skipped = [c for c in checks if c.state == NOT_APPLICABLE]
    scored = [c for c in checks if c.state == SCORED]

    by_unit = {}
    for c in scored:
        acc = by_unit.setdefault(c.unit, {"opportunities": 0, "defects": 0, "keys": []})
        acc["opportunities"] += c.opportunities
        acc["defects"] += c.defects
        acc["keys"].append(c.key)
    for unit, acc in by_unit.items():
        rolled = Check("_rollup_" + unit, "rollup", acc["opportunities"], acc["defects"], unit)
        acc["dpmo"] = rolled.dpmo
        acc["sigma"] = rolled.sigma

    out = {"checks_total": len(checks),
           "checks_scored": len(scored),
           "checks_not_applicable": [c.key for c in skipped],
           "checks_unknown": [c.key for c in unknown],
           "by_unit": by_unit,
           "cross_unit_total": None}
    if unknown:
        out["verdict"] = ("INCOMPLETE - {0} check(s) could not be established: {1}. "
                          "This run does not know what it did not measure, and must "
                          "not be read as a score.").format(
                              len(unknown), ", ".join(c.key for c in unknown))
    elif not scored:
        out["verdict"] = ("NOT SCOREABLE - every check had an empty population. This is "
                          "not a clean org; it is an unmeasured one.")
    else:
        out["verdict"] = "scored"
    return out


def load_env(path):
    conf = {}
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            conf[k.strip()] = v.strip().strip('"').strip("'")
    return conf


# Only these suffixes are accepted as the org host. A client secret is about to
# be posted to it, so "looks like a URL" is not good enough.
SALESFORCE_HOSTS = (".salesforce.com", ".force.com", ".salesforce.mil")


def normalise_base(domain):
    """HTTPS to a Salesforce host, or nothing.

    The old version prepended https:// only when the string did not already
    start with "http" - so a value of http://evil.example passed straight
    through as PLAINTEXT, and the client secret below would have gone with it.
    A config file is not a trust boundary; this is.
    """
    base = str(domain).strip().rstrip("/")
    if not base:
        raise ValueError("no org domain configured")
    if base.startswith("http://"):
        raise ValueError("refusing a plaintext http:// org domain - a client "
                         "secret is posted to this host")
    if not base.startswith("https://"):
        base = "https://" + base
    host = (urllib.parse.urlparse(base).hostname or "").lower()
    if not host:
        raise ValueError("could not parse a hostname from the org domain")
    if not host.endswith(SALESFORCE_HOSTS):
        raise ValueError("refusing to send client credentials to a non-Salesforce "
                         "host: " + host)
    return base


ORG_ID = re.compile(r"^00D[0-9A-Za-z]{12}(?:[0-9A-Za-z]{3})?$")
USER_ID = re.compile(r"^005[0-9A-Za-z]{12}(?:[0-9A-Za-z]{3})?$")


def identity_from(token_response):
    """The org and the principal this token actually belongs to.

    The token response carries `id`, of the form
    https://login.salesforce.com/id/<orgId>/<userId>. It was being discarded, so
    the tool scored whatever org the credentials happened to open and the report
    said only what the CONFIG claimed. Swap the client_id for another org's and
    every number changes while the header stays the same - a scoring engine that
    cannot say which org it scored is not evidence of anything.
    """
    raw = str(token_response.get("id") or "")
    parts = [p for p in urllib.parse.urlparse(raw).path.split("/") if p]
    # .../id/<org>/<user>
    if len(parts) < 3 or parts[0] != "id":
        return None, None
    org, user = parts[1], parts[2]
    return (org if ORG_ID.match(org) else None,
            user if USER_ID.match(user) else None)


def get_token(conf):
    """Exchange client credentials for a token, and learn WHOSE it is.

    HTTPS to a verified Salesforce host, and NO REDIRECTS. A redirect on this
    request would carry the client_id and client_secret to whatever host the
    Location header names - which is the worst possible thing to follow
    automatically. The Data API path was hardened first and this one was not;
    found by codex reviewing PR55 at 2c1ae2f.

    Returns (base, token, identity). The base is taken from `instance_url` when
    the org supplies one, because that is the host the token is actually good
    for; the configured domain only has to agree with it.
    """
    base = normalise_base(conf["Headless_domain"])
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": conf["Headless_consumer_key"],
        "client_secret": conf["Headless_consumer_secret"]}).encode()
    req = urllib.request.Request(base + "/services/oauth2/token", data=body)
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=45) as resp:
        payload = json.load(resp)

    org, user = identity_from(payload)

    # The org's own answer for where its API lives. Validated through the same
    # gate as the configured domain - it arrives over the network, so it is
    # input, not truth.
    instance = payload.get("instance_url")
    if instance:
        instance_base = normalise_base(instance)
        if instance_base != base:
            # Not fatal by itself: My Domain and instance_url legitimately
            # differ in some orgs. It IS reported, and the token's own host wins,
            # because that is the one the credentials were issued for.
            base = instance_base

    expected_org = (conf.get("Headless_expected_org_id") or "").strip()
    expected_user = (conf.get("Headless_expected_user_id") or "").strip()
    if expected_org:
        if not org:
            raise ValueError("an expected org id is configured but the token "
                             "response carried no readable org id; refusing to "
                             "score an org this run cannot identify")
        if not same_sf_id(org, expected_org):
            raise ValueError("this token belongs to a DIFFERENT org than the one "
                             "configured as expected; refusing to score it")
    if expected_user:
        if not user or not same_sf_id(user, expected_user):
            raise ValueError("this token's principal is not the configured "
                             "expected user; refusing to score under it")

    return base, payload["access_token"], {
        "org_id": org,
        "user_id": user,
        "instance_host": urllib.parse.urlparse(base).hostname,
        # The whole point of the field: a reader must be able to tell a run that
        # PROVED it hit the right org from one that merely hoped so.
        "binding": "VERIFIED" if expected_org else "UNVERIFIED",
        "binding_note": (
            "org id matched Headless_expected_org_id" if expected_org else
            "no Headless_expected_org_id configured, so nothing checked that "
            "these credentials open the org this report names"),
    }


def same_sf_id(a, b):
    """Salesforce ids come in 15- and 18-character forms for the same record.

    The 18-char form is the 15-char form plus a 3-character checksum, so a
    prefix comparison on the first 15 is the correct equality test. A raw string
    compare would call the same org two different orgs and fail a run that
    should pass - and, worse, invite someone to "fix" it by removing the check.
    The 15-char prefix is case-SENSITIVE; only the checksum suffix is not.
    """
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    if len(a) not in (15, 18) or len(b) not in (15, 18):
        return False
    return a[:15] == b[:15]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect can move a request to a host we never approved."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            "refusing to follow a redirect to " + str(newurl), headers, fp)


def get_json(base, token, path):
    """The ONLY way this file talks to the org. GET, to the expected host, no
    redirects, no body - enforced at runtime rather than asserted in a comment.

    A source-text assertion that "there are no POSTs" is bypassable by anyone
    who splits a line. This is the boundary itself.
    """
    if not path.startswith("/services/data/"):
        raise ValueError("refusing a non Data API path: " + path)
    url = base + path
    if not url.startswith(normalise_base(base) + "/"):
        raise ValueError("refusing a host that is not the expected org")
    if not url.startswith("https://"):
        raise ValueError("refusing a non-HTTPS request")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    if req.get_method() != "GET":
        raise ValueError("refusing a non-GET request")
    opener = urllib.request.build_opener(NoRedirect)
    with opener.open(req, timeout=60) as resp:
        return json.load(resp)


def count(base, token, soql):
    """COUNT() via SOQL.

    Returns None on failure and never 0. A failed query and an empty result are
    different facts, and only one of them is news about the org. The caller
    turns None into an UNKNOWN check rather than dropping it.
    """
    try:
        return get_json(base, token,
                        "/services/data/" + API + "/query?q=" + urllib.parse.quote(soql))["totalSize"]
    except Exception:
        return None


def build_checks(base, token, window_days=90, counter=None):
    """The checks, each stated as opportunity and defect.

    A check is ALWAYS emitted. If its queries failed it is emitted UNKNOWN, so a
    total outage cannot masquerade as an org with nothing to find.
    """
    q = counter or count
    w = "LAST_N_DAYS:" + str(window_days)
    checks = []

    eligible = q(base, token,
                 "SELECT COUNT() FROM User WHERE IsActive=true AND CreatedDate < " + w)
    dormant = q(base, token,
                "SELECT COUNT() FROM User WHERE IsActive=true AND CreatedDate < " + w
                + " AND (LastLoginDate = null OR LastLoginDate < " + w + ")")
    note = ""
    if eligible == 0:
        fresh = q(base, token,
                  "SELECT COUNT() FROM User WHERE IsActive=true AND CreatedDate >= " + w)
        note = ("every active user was provisioned inside the last {0} days{1}, so "
                "none has had the chance to go dormant yet").format(
                    window_days, " (" + str(fresh) + " of them)" if fresh else "")
    checks.append(Check(
        "dormant_users",
        "Active users with no login in {0} days".format(window_days),
        eligible, dormant, "user", note,
        reason="" if eligible is not None and dormant is not None
               else "a User COUNT() query did not return"))

    all_flows = q(base, token, "SELECT COUNT() FROM FlowDefinitionView")
    live_flows = q(base, token, "SELECT COUNT() FROM FlowDefinitionView WHERE IsActive=true")
    inactive = None
    if all_flows is not None and live_flows is not None:
        inactive = all_flows - live_flows
        if inactive < 0:
            # More active than total means the two queries disagree. That is a
            # broken measurement, not an org with negative dead flows.
            all_flows, inactive = None, None
    checks.append(Check(
        "inactive_flows", "Flow definitions that are not active",
        all_flows, inactive, "flow",
        reason="" if all_flows is not None
               else "a FlowDefinitionView COUNT() query did not return, or the "
                    "active count exceeded the total"))
    return checks


def render(org, checks, summary):
    out = []
    out.append("xray-score-v1")
    out.append("org: " + org)
    out.append("")
    for c in checks:
        out.append(c.title)
        if c.state == UNKNOWN:
            out.append("  state         : UNKNOWN")
            out.append("  why           : " + (c.reason or "could not be established"))
            out.append("")
            continue
        out.append("  opportunities : {0} {1}(s)".format(c.opportunities, c.unit))
        out.append("  defects       : {0}".format(c.defects))
        if c.state == SCORED:
            sig = c.sigma
            sigtxt = "unbounded" if sig["value"] is None else "{0:.2f}".format(sig["value"])
            out.append("  DPMO          : {0:,}".format(c.dpmo))
            out.append("  sigma         : " + sigtxt + ((" (" + sig["why"] + ")") if sig["why"] else ""))
        else:
            out.append("  state         : NOT APPLICABLE")
            out.append("  why           : " + (c.note or "no opportunities to score"))
        out.append("")
    out.append("ROLLUP, per unit - opportunities of different kinds are not added")
    if summary["by_unit"]:
        for unit, acc in sorted(summary["by_unit"].items()):
            sig = acc["sigma"]
            sigtxt = "unbounded" if sig["value"] is None else "{0:.2f}".format(sig["value"])
            out.append("  {0:<8} {1:,} DPMO over {2:,} opportunities | sigma {3}".format(
                unit, acc["dpmo"], acc["opportunities"], sigtxt))
    else:
        out.append("  nothing was scoreable")
    if summary["checks_unknown"]:
        out.append("  UNKNOWN: " + ", ".join(summary["checks_unknown"]))
    if summary["checks_not_applicable"]:
        out.append("  not applicable: " + ", ".join(summary["checks_not_applicable"]))
    if summary["verdict"] != "scored":
        out.append("")
        out.append("  " + summary["verdict"])
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="xray-score-v1 (read-only)")
    ap.add_argument("--env", default=None, help="path to .env (default: repo root)")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--window", type=int, default=90)
    args = ap.parse_args()

    env_path = args.env
    if not env_path:
        import os
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    conf = load_env(env_path)
    for k in ("Headless_domain", "Headless_consumer_key", "Headless_consumer_secret"):
        if not conf.get(k):
            sys.exit("missing {0} in {1} - credentials are read from .env only (D-18)".format(k, env_path))

    base, token, identity = get_token(conf)
    checks = build_checks(base, token, args.window)
    summary = summarise(checks)
    print(render(conf["Headless_domain"], checks, summary))
    print("")
    print("scored org {0} as principal {1} on {2}".format(
        identity["org_id"] or "UNKNOWN", identity["user_id"] or "UNKNOWN",
        identity["instance_host"]))
    print("identity binding: {0} - {1}".format(
        identity["binding"], identity["binding_note"]))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"org": conf["Headless_domain"],
                       # Which org this actually was, not which one was asked for.
                       "identity": identity,
                       "window_days": args.window,
                       "checks": [c.to_dict() for c in checks],
                       "summary": summary}, fh, indent=2)
        print("\nwrote " + args.json_out)
    # Non-zero when the run did not establish what it claims. A scoring tool
    # that exits 0 after failing to reach the org is the defect this fixes.
    return 2 if summary["checks_unknown"] else 0


if __name__ == "__main__":
    sys.exit(main())
