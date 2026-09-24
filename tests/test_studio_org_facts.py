"""org_facts: read-only, template-only, developer-org-only. A fake opener stands in for Salesforce."""
import importlib.util
import io
import json
import unittest
import urllib.error
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "org_facts", REPO / "cloud" / "studio-controller" / "workers" / "org_facts.py")
of = importlib.util.module_from_spec(spec)
spec.loader.exec_module(of)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_org(calls):
    def opener(req, timeout=None):
        calls.append(req)
        if req.full_url.endswith("/services/oauth2/token"):
            return Resp(json.dumps({"instance_url": "https://x.develop.my.salesforce.com",
                                    "access_token": "t"}).encode())
        q = urllib.parse.unquote(req.full_url.split("q=", 1)[1])
        if "FROM Organization" in q:
            body = {"records": [{"Id": "00Dx", "Name": "SFDC 24", "OrganizationType": "Developer Edition"}]}
        elif "GROUP BY LeadSource" in q:
            body = {"records": [{"LeadSource": "sfdc24.com", "n": 4}, {"LeadSource": None, "n": 22}]}
        elif "LAST_N_DAYS" in q:
            body = {"totalSize": 1, "records": []}
        else:
            body = {"totalSize": 26, "records": []}
        return Resp(json.dumps(body).encode())
    return opener


class OrgFactsTest(unittest.TestCase):
    def facts(self, calls):
        return of.OrgFacts("dbm00000wk2ibeac-dev-ed.develop.my.salesforce.com", "id", "secret", fake_org(calls))

    def test_lead_counts_name_the_org_the_source_and_the_time(self):
        calls = []
        a = self.facts(calls).lead_counts()
        self.assertEqual((a["total"], a["site_total"], a["site_last_7_days"]), (26, 4, 1))
        self.assertEqual(a["org_name"], "SFDC 24")
        self.assertIn("(none)", a["by_source"])
        text = of.describe_lead_counts(a)
        for piece in ("SFDC 24", "26 leads", "4 came from the sfdc24.com site", "Live count", a["observed_at"]):
            self.assertIn(piece, text)

    def test_only_fixed_read_templates_ever_run(self):
        calls = []
        self.facts(calls).lead_counts()
        sent = [urllib.parse.unquote(c.full_url.split("q=", 1)[1]) for c in calls if "q=" in c.full_url]
        self.assertEqual(sorted(sent), sorted(of.QUERIES.values()))
        for q in of.QUERIES.values():
            self.assertTrue(q.lstrip().upper().startswith("SELECT"), q)
        for c in calls:
            if "q=" in c.full_url:
                self.assertEqual(c.get_method(), "GET")
        with self.assertRaises(KeyError):
            self.facts([])._query("DELETE FROM Lead")

    def test_a_customer_tenant_is_refused(self):
        with self.assertRaises(ValueError):
            of.OrgFacts("acme.my.salesforce.com", "id", "secret", fake_org([]))

    def test_missing_credentials_fail_plainly(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in ("Headless_domain", "Headless_consumer_key", "Headless_consumer_secret")}
        try:
            with self.assertRaises(RuntimeError):
                of.OrgFacts.from_env()
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_lead_questions_are_recognised_and_others_are_not(self):
        for yes in ("How many leads do we have?", "what's the total number of leads", "count of leads please",
                    "any leads so far today?"):
            self.assertTrue(of.is_lead_count_question(yes), yes)
        for no in ("How do I convert a lead?", "Should the hero lead with a question?", "Lead assignment rules"):
            self.assertFalse(of.is_lead_count_question(no), no)


class CodexReReview200OrgFacts(unittest.TestCase):
    """CODEX-PR200-REREVIEW2-20260924T0518Z, P1: nothing but an approved dev/sandbox org gets the bearer."""

    def test_spoofed_and_malformed_domains_are_refused(self):
        for bad in ("develop.my.salesforce.com.evil.test", "xdevelop.my.salesforce.com",
                    "https://user:pw@a.develop.my.salesforce.com", "http://a.develop.my.salesforce.com",
                    "https://a.develop.my.salesforce.com:8443", "https://a.develop.my.salesforce.com/path",
                    "acme.my.salesforce.com"):
            with self.assertRaises(ValueError, msg=bad):
                of.OrgFacts(bad, "id", "secret", fake_org([]))

    def test_an_instance_url_elsewhere_never_receives_the_bearer(self):
        calls = []
        def opener(req, timeout=None):
            calls.append(req)
            return Resp(json.dumps({"instance_url": "https://customer.my.salesforce.com",
                                    "access_token": "t"}).encode())
        f = of.OrgFacts("a.develop.my.salesforce.com", "id", "secret", opener)
        with self.assertRaises(ValueError):
            f.lead_counts()
        self.assertEqual(len(calls), 1, "only the token request was made")

    def test_a_production_org_is_refused_before_any_lead_is_read(self):
        calls = []
        base = fake_org(calls)
        def opener(req, timeout=None):
            if "FROM%20Organization" in req.full_url or "FROM Organization" in urllib.parse.unquote(req.full_url):
                calls.append(req)
                return Resp(json.dumps({"records": [{"Id": "00Dp", "Name": "Acme",
                                                     "OrganizationType": "Enterprise Edition", "IsSandbox": False}]}).encode())
            return base(req, timeout)
        f = of.OrgFacts("a.develop.my.salesforce.com", "id", "secret", opener)
        with self.assertRaises(PermissionError):
            f.lead_counts()
        self.assertFalse(any("FROM Lead" in urllib.parse.unquote(c.full_url) for c in calls if "q=" in c.full_url))

    def test_the_default_opener_does_not_follow_redirects(self):
        f = of.OrgFacts("a.develop.my.salesforce.com", "id", "secret")
        self.assertIsNotNone(f._open)
        h = of._NoRedirect()
        self.assertIsNone(h.redirect_request(None, None, 302, "Found", {}, "https://elsewhere.test/"))


def failing_token(codes, calls):
    """The fake org, except the first len(codes) token calls fail with those HTTP codes."""
    base = fake_org(calls)
    pending = list(codes)
    def opener(req, timeout=None):
        if req.full_url.endswith("/services/oauth2/token") and pending:
            calls.append(req)
            raise urllib.error.HTTPError(req.full_url, pending.pop(0), "x", {}, io.BytesIO(b""))
        return base(req, timeout)
    return opener


class TokenRetry(unittest.TestCase):
    """The first live run after ca30068 hit a one-off token 404 (as org_snapshot did
    on 2026-09-17); three reruns a minute later all answered."""

    def facts(self, opener):
        f = of.OrgFacts("a.develop.my.salesforce.com", "id", "secret", opener)
        f._sleep = lambda s: None
        return f

    def token_calls(self, calls):
        return sum(1 for c in calls if c.full_url.endswith("/services/oauth2/token"))

    def test_a_transient_404_is_retried_and_the_answer_arrives(self):
        calls = []
        a = self.facts(failing_token([404], calls)).lead_counts()
        self.assertEqual(a["total"], 26)
        self.assertEqual(self.token_calls(calls), 2)

    def test_bad_credentials_are_never_retried(self):
        for code in (400, 401, 403):
            calls = []
            with self.assertRaises(urllib.error.HTTPError):
                self.facts(failing_token([code, code, code], calls)).lead_counts()
            self.assertEqual(self.token_calls(calls), 1, code)

    def test_retries_are_bounded(self):
        calls = []
        with self.assertRaises(urllib.error.HTTPError):
            self.facts(failing_token([404] * 10, calls)).lead_counts()
        self.assertEqual(self.token_calls(calls), 3)
        self.assertFalse(any("q=" in c.full_url for c in calls), "no query ran without a token")


if __name__ == "__main__":
    unittest.main()
