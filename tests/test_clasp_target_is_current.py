#!/usr/bin/env python3
"""`clasp push` from this repo would delete a provider from the live site.

.clasp.json points at apps-script/governor-page-api/Code.gs. That file has no
grok routing at all. The live site answered ten real questions on 2026-09-20 and
every single one came back by=grok, so the deployed script plainly has routing
the push target does not.

Both files open with the identical header line, so nothing warns you.

This suite does not try to guess which source is right - that needs the
deployment-id-to-script-id mapping nobody has. It asserts the one thing that is
safe to assert: while the push target is missing a provider the live site is
known to use, a document must exist saying so, and it must name the provider.
A hazard that lives only in someone's memory is not a guarded hazard.

Run: python tests/test_clasp_target_is_current.py
"""

import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEPLOY_DOC = os.path.join(REPO, "docs", "APPS-SCRIPT-DEPLOY.md")

# Measured live on 2026-09-20: ten questions, ten replies, every one by=grok.
# The marker is the credential name, because that is what the routing branches
# on and it cannot be renamed without someone noticing this test.
PROVIDERS_THE_LIVE_SITE_USES = {
    "grok": "XAI_API_KEY",
    "claude": "ANTHROPIC_KEY",
}


def clasp_target_source() -> str:
    with open(os.path.join(REPO, ".clasp.json"), encoding="utf-8") as fh:
        root = json.load(fh).get("rootDir", "")
    d = os.path.join(REPO, root)
    if not os.path.isdir(d):
        return ""
    parts = []
    for name in sorted(os.listdir(d)):
        if name.endswith((".gs", ".js")):
            with open(os.path.join(d, name), encoding="utf-8", errors="replace") as fh:
                parts.append(fh.read())
    return "\n".join(parts)


class TheHazardIsWrittenDown(unittest.TestCase):
    def test_every_missing_provider_is_named_in_the_deploy_doc(self):
        src = clasp_target_source()
        self.assertTrue(src, "clasp rootDir holds no .gs/.js source at all")

        missing = [name for name, marker in PROVIDERS_THE_LIVE_SITE_USES.items()
                   if marker not in src]
        if not missing:
            return  # reconciled; nothing to warn about

        self.assertTrue(
            os.path.isfile(DEPLOY_DOC),
            "the clasp push target is missing provider(s) %s that the live site "
            "uses, and docs/APPS-SCRIPT-DEPLOY.md does not exist to say so. "
            "Pushing would remove them silently." % missing)

        with open(DEPLOY_DOC, encoding="utf-8") as fh:
            doc = fh.read().lower()
        for name in missing:
            with self.subTest(provider=name):
                self.assertIn(
                    name, doc,
                    "%s routing is absent from the push target and unmentioned "
                    "in the deploy doc" % name)

    def test_the_doc_tells_a_reader_not_to_push(self):
        if not os.path.isfile(DEPLOY_DOC):
            self.skipTest("no deploy doc, and the test above owns that case")
        with open(DEPLOY_DOC, encoding="utf-8") as fh:
            doc = fh.read().lower()
        self.assertIn("clasp push", doc)
        self.assertTrue(re.search(r"\bdo not\b|\bunsafe\b|\bbefore anyone pushes\b", doc),
                        "the doc must actually say not to push, not merely describe")


class TheVersionHeaderCannotBeTrusted(unittest.TestCase):
    def test_a_header_match_is_not_evidence_of_sameness(self):
        """Recorded so nobody uses the header as a freshness check again.

        apps-script/governor-page-api/Code.gs and the untracked gas/Code.js both
        begin 'SFDC24 - site engine (v3 . 2026-09-02)' and differ by ~3KB,
        including an entire model provider.
        """
        src = clasp_target_source()
        if not src:
            self.skipTest("no source")
        self.assertIn("site engine", src,
                      "if this header changes, re-check the assumption this "
                      "suite is built on")


if __name__ == "__main__":
    unittest.main(verbosity=2)
