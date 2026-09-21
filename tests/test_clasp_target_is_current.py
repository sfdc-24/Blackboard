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
            return  # reconciled on 2026-09-21; nothing to warn about

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

    def test_the_doc_states_the_standing_rule(self):
        """It used to have to say "do not push". Now it must say what
        replaced that: pull first, because this directory mirrors a live
        system that can be edited in a browser by someone who never touches
        git."""
        if not os.path.isfile(DEPLOY_DOC):
            self.skipTest("no deploy doc, and the test above owns that case")
        with open(DEPLOY_DOC, encoding="utf-8") as fh:
            doc = fh.read().lower()
        self.assertIn("clasp push", doc)
        self.assertIn("clasp pull", doc)
        self.assertIn("pull before you push", doc,
                      "the standing rule has to be in the document, not in "
                      "somebody's memory")


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


class ThePushTargetIsTheLiveScript(unittest.TestCase):
    """Confirmed by Mr Salam 2026-09-21, so these are no longer precautions.

    The live /exec the homepage calls - deployment AKfycbx0D-5DAnMq... - belongs
    to script 1lTbqTZ3..., which is exactly the scriptId in .clasp.json. So this
    directory is not a copy of production; pushing it IS a deploy.
    """

    def test_every_provider_the_live_site_uses_is_in_the_push_target(self):
        """Before reconciliation the target had no grok at all, while ten live
        replies came back by=grok. A push would have removed a provider."""
        src = clasp_target_source()
        for name, marker in sorted(PROVIDERS_THE_LIVE_SITE_USES.items()):
            with self.subTest(provider=name):
                self.assertIn(marker, src,
                              "%s routing is missing from the clasp push target; "
                              "pushing would remove it from sfdc24.com" % name)

    def test_the_target_is_a_whole_project_not_one_file(self):
        """clasp push sends the rootDir as the entire project. A rootDir holding
        fewer files than the live script deletes the rest - the reception page,
        the governor auth, the monitor."""
        import json as _json
        with open(os.path.join(REPO, ".clasp.json"), encoding="utf-8") as fh:
            root = _json.load(fh).get("rootDir", "")
        d = os.path.join(REPO, root)
        names = {n for n in os.listdir(d)
                 if n.endswith((".js", ".gs", ".html", ".json"))}
        for required in ("appsscript.json",):
            self.assertIn(required, names)
        self.assertGreaterEqual(
            len(names), 7,
            "the live script has 7 files; a rootDir with fewer would delete the "
            "difference on push. Found: %s" % sorted(names))

    def test_no_stale_duplicate_extension_is_left_behind(self):
        """Auth.gs and Auth.js in one rootDir is two copies of one server file.
        clasp would push both and the older can win."""
        import json as _json
        with open(os.path.join(REPO, ".clasp.json"), encoding="utf-8") as fh:
            root = _json.load(fh).get("rootDir", "")
        d = os.path.join(REPO, root)
        stems = {}
        for n in os.listdir(d):
            stem, dot, ext = n.rpartition(".")
            if ext in ("js", "gs"):
                stems.setdefault(stem, []).append(ext)
        dupes = {k: v for k, v in stems.items() if len(v) > 1}
        self.assertEqual(dupes, {}, "same server file twice: %s" % dupes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
