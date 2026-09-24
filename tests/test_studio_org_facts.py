"""org_facts: read-only, template-only, developer-org-only. A fake opener stands in for Salesforce."""
import importlib.util
import io
import json
import unittest
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


if __name__ == "__main__":
    unittest.main()
