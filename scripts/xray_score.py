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
  A check that cannot tell must SAY SO.

  Measured 2026-09-09 against the real workshop org: the first version counted
  "never logged in" as "stale", found 9 of 10 active users had never logged in,
  and reported 900,000 DPMO and sigma 0.22. Every one of those users had been
  created inside the 90-day window - none had ever had the chance to log in. The
  defect rate was computed over a population that could not contain a defect.

  On a paying customer that is a confident, precise, wrong answer given to the
  person paying for the answer. So a Check carries OPPORTUNITIES and DEFECTS
  separately, and where there are no opportunities it returns NOT_APPLICABLE -
  not zero, not a DPMO, not a sigma. Scoring nothing is not the same as scoring
  perfectly, and telling those two apart is the product.

USAGE
  python3 scripts/xray_score.py
  python3 scripts/xray_score.py --json out.json
  python3 scripts/xray_score.py --env /path/to/.env --window 90

  Credentials come from .env (D-18): Headless_domain, Headless_consumer_key,
  Headless_consumer_secret. Never from argv, never echoed.

READ-ONLY BY CONSTRUCTION
  Every call here is a GET against the Data API. There is no write path in this
  file and there must never be one - a scoring engine that can change the org it
  is scoring is a different and far more dangerous product.
"""
import argparse
import json
import math
import sys
import urllib.parse
import urllib.request

API = "v67.0"
NOT_APPLICABLE = "NOT_APPLICABLE"


class Check:
    """One measurable thing, as opportunities and defects.

    `opportunities` is the population that COULD have been a defect. `defects`
    is how many of them were. Keeping them apart is the whole point: a defect
    count without its denominator is a number without a meaning.
    """

    def __init__(self, key, title, opportunities, defects, unit="record", note=""):
        if defects < 0 or opportunities < 0:
            raise ValueError(key + ": counts cannot be negative")
        if defects > opportunities:
            raise ValueError(
                "{0}: {1} defects out of {2} opportunities - more defects than "
                "chances to have one means the query is wrong, not that the org "
                "is broken".format(key, defects, opportunities))
        self.key = key
        self.title = title
        self.opportunities = opportunities
        self.defects = defects
        self.unit = unit
        self.note = note

    @property
    def scoreable(self):
        return self.opportunities > 0

    @property
    def dpmo(self):
        if not self.scoreable:
            return NOT_APPLICABLE
        return round(self.defects / self.opportunities * 1000000)

    @property
    def sigma(self):
        if not self.scoreable:
            return NOT_APPLICABLE
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
                "opportunities": self.opportunities, "defects": self.defects,
                "scoreable": self.scoreable, "dpmo": self.dpmo,
                "sigma": self.sigma, "note": self.note}


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
    """Roll checks up WITHOUT silently dropping the unscoreable ones.

    An unscoreable check that quietly contributes 0/0 would drag a rollup toward
    "perfect" for the reason that nothing was measured. They are named instead.
    """
    scoreable = [c for c in checks if c.scoreable]
    skipped = [c for c in checks if not c.scoreable]
    opp = sum(c.opportunities for c in scoreable)
    bad = sum(c.defects for c in scoreable)
    out = {"checks_total": len(checks),
           "checks_scored": len(scoreable),
           "checks_not_applicable": [c.key for c in skipped],
           "opportunities": opp,
           "defects": bad}
    if opp == 0:
        out["dpmo"] = NOT_APPLICABLE
        out["sigma"] = NOT_APPLICABLE
        out["verdict"] = ("NOT SCOREABLE - no check in this org had a population "
                          "that could contain a defect. This is not a clean org; "
                          "it is an unmeasured one.")
        return out
    rolled = Check("_rollup", "rollup", opp, bad)
    out["dpmo"] = rolled.dpmo
    out["sigma"] = rolled.sigma
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


def get_token(conf):
    base = conf["Headless_domain"].rstrip("/")
    if not base.startswith("http"):
        base = "https://" + base
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": conf["Headless_consumer_key"],
        "client_secret": conf["Headless_consumer_secret"]}).encode()
    req = urllib.request.Request(base + "/services/oauth2/token", data=body)
    with urllib.request.urlopen(req, timeout=45) as resp:
        return base, json.load(resp)["access_token"]


def count(base, token, soql):
    """COUNT() via SOQL.

    Returns None on failure and never 0. A failed query and an empty result are
    different facts, and only one of them is news about the org.
    """
    url = base + "/services/data/" + API + "/query?q=" + urllib.parse.quote(soql)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)["totalSize"]
    except Exception:
        return None


def build_checks(base, token, window_days=90):
    """The checks, each stated as opportunity and defect."""
    w = "LAST_N_DAYS:" + str(window_days)
    checks = []

    # Dormant users. The opportunity is users who have HAD the window in which
    # to log in. A user created inside the window has not, and is not a defect.
    eligible = count(base, token,
                     "SELECT COUNT() FROM User WHERE IsActive=true AND CreatedDate < " + w)
    dormant = count(base, token,
                    "SELECT COUNT() FROM User WHERE IsActive=true AND CreatedDate < " + w
                    + " AND (LastLoginDate = null OR LastLoginDate < " + w + ")")
    if eligible is not None and dormant is not None:
        note = ""
        if eligible == 0:
            fresh = count(base, token,
                          "SELECT COUNT() FROM User WHERE IsActive=true AND CreatedDate >= " + w)
            note = ("every active user was provisioned inside the last {0} days"
                    "{1}, so none has had the chance to go dormant yet").format(
                        window_days, " (" + str(fresh) + " of them)" if fresh else "")
        checks.append(Check("dormant_users",
                            "Active users with no login in {0} days".format(window_days),
                            eligible, dormant, "user", note))

    # Dead automation an admin has to read past every time they open the org.
    all_flows = count(base, token, "SELECT COUNT() FROM FlowDefinitionView")
    live_flows = count(base, token,
                       "SELECT COUNT() FROM FlowDefinitionView WHERE IsActive=true")
    if all_flows is not None and live_flows is not None:
        checks.append(Check("inactive_flows", "Flow definitions that are not active",
                            all_flows, all_flows - live_flows, "flow"))
    return checks


def render(org, checks, summary):
    out = []
    out.append("xray-score-v1")
    out.append("org: " + org)
    out.append("")
    for c in checks:
        out.append(c.title)
        out.append("  opportunities : {0} {1}(s)".format(c.opportunities, c.unit))
        out.append("  defects       : {0}".format(c.defects))
        if c.scoreable:
            sig = c.sigma
            sigtxt = "unbounded" if sig["value"] is None else "{0:.2f}".format(sig["value"])
            out.append("  DPMO          : {0:,}".format(c.dpmo))
            out.append("  sigma         : " + sigtxt + (("  (" + sig["why"] + ")") if sig["why"] else ""))
        else:
            out.append("  DPMO          : NOT APPLICABLE")
            out.append("  why           : " + (c.note or "no opportunities to score"))
        out.append("")
    out.append("ROLLUP")
    if summary["verdict"] == "scored":
        sig = summary["sigma"]
        sigtxt = "unbounded" if sig["value"] is None else "{0:.2f}".format(sig["value"])
        # ASCII only. A U+00B7 separator here rendered as a replacement
        # character on the Windows console codepage and would corrupt the output
        # of any pipe or CI capture. This tool has to read the same everywhere.
        out.append("  {0:,} DPMO over {1:,} opportunities | sigma {2}".format(
            summary["dpmo"], summary["opportunities"], sigtxt))
    else:
        out.append("  " + summary["verdict"])
    if summary["checks_not_applicable"]:
        out.append("  not applicable: " + ", ".join(summary["checks_not_applicable"]))
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

    base, token = get_token(conf)
    checks = build_checks(base, token, args.window)
    summary = summarise(checks)
    print(render(conf["Headless_domain"], checks, summary))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"org": conf["Headless_domain"],
                       "window_days": args.window,
                       "checks": [c.to_dict() for c in checks],
                       "summary": summary}, fh, indent=2)
        print("\nwrote " + args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
