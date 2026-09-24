"""Live, read-only facts from the SFDC24 developer org, for the studio.

"How many leads do we have?" must be answered from the org, not from a model's
guess or a snapshot - with the org, what was counted, and when, so the answer
can be checked. This module is the only path from the studio to the org, and it
is deliberately narrow:

  - READ ONLY. Only fixed SOQL templates defined here run. A model never writes
    SOQL, and nothing here can insert, update, delete or deploy.
  - DEVELOPER OR SANDBOX ORGS ONLY. The host must look like one (the same guard
    scripts/org_snapshot.py uses), so a mis-set secret cannot point it at a
    customer tenant.
  - CREDENTIALS FROM THE ENVIRONMENT (Secret Manager in Cloud Run):
    Headless_domain, Headless_consumer_key, Headless_consumer_secret - the
    client-credentials connected app already used for the dev org.

    facts = OrgFacts.from_env()
    answer = facts.lead_counts()        # dict with org, counts, source, observed_at
    text = describe_lead_counts(answer) # one plain sentence for the page and the voice
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "v62.0"
# Host SUFFIXES, matched on a dot boundary - "develop.my.salesforce.com.evil.test"
# and "xdevelop.my.salesforce.com" must not pass (Codex re-review of PR 200, P1).
ALLOWED_HOST_SUFFIXES = (".develop.my.salesforce.com", ".sandbox.my.salesforce.com",
                         ".scratch.my.salesforce.com", ".trailblaze.my.salesforce.com")
ALLOWED_ORG_TYPES = ("Developer Edition",)
SITE_SOURCE = "sfdc24.com"

# The whole query surface. Adding a fact means adding a template here, in review.
QUERIES = {
    "org": "SELECT Id, Name, OrganizationType, IsSandbox FROM Organization LIMIT 1",
    "lead_total": "SELECT COUNT() FROM Lead",
    "lead_by_source": "SELECT LeadSource, COUNT(Id) n FROM Lead GROUP BY LeadSource",
    "lead_site_recent": ("SELECT COUNT() FROM Lead WHERE LeadSource = 'sfdc24.com' "
                         "AND CreatedDate = LAST_N_DAYS:7"),
}

LEAD_QUESTION = re.compile(
    r"\b(how many|number of|count of|count|total)\b[^?.!]{0,40}\bleads?\b|\bleads?\b[^?.!]{0,30}\b(so far|today|this week|do we have|have we got)\b",
    re.I)


def is_lead_count_question(text: str) -> bool:
    return bool(LEAD_QUESTION.search(text or ""))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A bearer token must never follow a redirect to another host."""
    def redirect_request(self, *a, **k):
        return None


def _approved_origin(url: str) -> str:
    """https://<host> for an approved developer or sandbox host, else ValueError.
    No userinfo, no port, no path, no plain http."""
    u = urllib.parse.urlsplit(url.strip())
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port or u.path not in ("", "/") \
            or u.query or u.fragment:
        raise ValueError("refusing %r: must be a bare https origin" % url)
    if not any(host.endswith(sfx) for sfx in ALLOWED_HOST_SUFFIXES):
        raise ValueError("refusing %s: not a developer or sandbox org" % host)
    return "https://" + host


class OrgFacts:
    def __init__(self, domain: str, client_id: str, client_secret: str, opener=None):
        dom = domain.strip()
        if "://" not in dom:
            dom = "https://" + dom
        self.domain = _approved_origin(dom)
        self._cid, self._sec = client_id, client_secret
        self._open = opener or urllib.request.build_opener(_NoRedirect).open
        self._token = None
        self.host = urllib.parse.urlsplit(self.domain).hostname

    @classmethod
    def from_env(cls, opener=None):
        missing = [k for k in ("Headless_domain", "Headless_consumer_key", "Headless_consumer_secret")
                   if not os.environ.get(k)]
        if missing:
            raise RuntimeError("org facts unavailable: %s not set" % ", ".join(missing))
        return cls(os.environ["Headless_domain"], os.environ["Headless_consumer_key"],
                   os.environ["Headless_consumer_secret"], opener)

    def _auth(self):
        if self._token:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "client_credentials",
                                       "client_id": self._cid, "client_secret": self._sec}).encode()
        req = urllib.request.Request(self.domain + "/services/oauth2/token", data=body, method="POST")
        with self._open(req, timeout=30) as r:
            tok = json.loads(r.read().decode("utf-8", "replace"))
        # The response names where to send the bearer; it gets the same check
        # as the configured domain before a token goes anywhere.
        self._token = (_approved_origin(tok["instance_url"]), tok["access_token"])
        return self._token

    def _query(self, name: str) -> dict:
        soql = QUERIES[name]  # KeyError for anything not in the template set - by design
        inst, tok = self._auth()
        url = "%s/services/data/%s/query?q=%s" % (inst, API, urllib.parse.quote(soql))
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
        with self._open(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def lead_counts(self) -> dict:
        org = (self._query("org").get("records") or [{}])[0]
        # Identity before data: a host that looks right is not proof. Refuse
        # anything but a developer org or a sandbox before a Lead is read.
        if not (org.get("IsSandbox") is True or org.get("OrganizationType") in ALLOWED_ORG_TYPES):
            raise PermissionError("refusing %s: organization type %r, sandbox %r"
                                  % (self.host, org.get("OrganizationType"), org.get("IsSandbox")))
        total = self._query("lead_total").get("totalSize", 0)
        by_source = {}
        for rec in self._query("lead_by_source").get("records") or []:
            by_source[rec.get("LeadSource") or "(none)"] = rec.get("n", 0)
        recent = self._query("lead_site_recent").get("totalSize", 0)
        return {
            "org_id": org.get("Id", ""), "org_name": org.get("Name", ""),
            "org_type": org.get("OrganizationType", ""), "host": self.host,
            "total": total, "by_source": by_source,
            "site_total": by_source.get(SITE_SOURCE, 0), "site_last_7_days": recent,
            "source": "live SOQL on the Lead object",
            "observed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def describe_lead_counts(a: dict) -> str:
    """One sentence a visitor can check: numbers, which org, what counted, when."""
    return ("%(org_name)s (%(org_type)s) has %(total)d leads in total; %(site_total)d came from "
            "the sfdc24.com site, %(site_last_7_days)d of them in the last 7 days. Live count from "
            "the Lead object at %(observed_at)s." % a)
