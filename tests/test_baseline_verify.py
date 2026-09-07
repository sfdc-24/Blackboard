#!/usr/bin/env python3
"""Acceptance for scripts/baseline_verify.py.

These exist because the first version of the canonical-JSON fix imported
canonical() but NOT parse_json(), so json.load silently kept the LAST of a
duplicate key and a manifest the deployment-identity path REFUSES was still
reported MATCH. Requested by codex-site-resume in SITE-BASELINE-REVIEW-20260907T231930Z.

The property under test is not "the digest is right". It is that this checker
can never accept a manifest the deploy path would reject.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
SOURCE_DIR = os.path.join(REPO, "apps-script", "governor-page-api")
sys.path.insert(0, SCRIPTS)

import baseline_verify  # noqa: E402  (path set above)


def status_of(rows, stem):
    for got_stem, _fn, status, _want, _got, _how in rows:
        if got_stem == stem:
            return status
    raise AssertionError(f"stem {stem} not reported at all")


class BaselineVerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="baseline_verify_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.dir = os.path.join(self.tmp, "governor-page-api")
        shutil.copytree(SOURCE_DIR, self.dir)
        self.manifest = os.path.join(self.dir, "appsscript.json")

    def write_manifest(self, text):
        with open(self.manifest, "w", encoding="utf-8") as fh:
            fh.write(text)

    def read_manifest(self):
        with open(self.manifest, encoding="utf-8-sig") as fh:
            return fh.read()

    # -- the baseline still works -------------------------------------------

    def test_unmodified_manifest_matches(self):
        rows, _ = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "MATCH")

    def test_reformatting_is_not_drift(self):
        """Different key order and indentation must NOT read as drift."""
        parsed = json.loads(self.read_manifest())
        reordered = dict(reversed(list(parsed.items())))
        self.write_manifest(json.dumps(reordered, indent=4) + "\n")
        rows, _ = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "MATCH")

    # -- the checker must not accept what the deploy path rejects ------------

    def test_duplicate_key_cannot_produce_match(self):
        """The exact attack from the review: corrupted FIRST value, real one last.

        json.load keeps the last and yields the correct digest. parse_json
        refuses the text outright.
        """
        text = self.read_manifest()
        self.assertIn('"runtimeVersion"', text)
        attacked = text.replace("{", '{"runtimeVersion":"CORRUPTED",', 1)

        # Prove the attack is real: the lenient parse still yields the good digest.
        import hashlib
        from gas_build_identity import canonical
        lenient = hashlib.sha256(
            canonical(json.loads(attacked)).encode("utf-8")).hexdigest()
        self.assertEqual(lenient, baseline_verify.DIGESTS_V31["appsscript"],
                         "fixture no longer reproduces the silent-duplicate defect")

        self.write_manifest(attacked)
        rows, mismatches = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "UNPARSEABLE")
        self.assertGreater(mismatches, 0)

    def test_nonstandard_number_cannot_produce_match(self):
        text = self.read_manifest().rstrip()
        self.assertTrue(text.endswith("}"))
        self.write_manifest(text[:-1] + ', "spendCap": NaN}')
        rows, _ = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "UNPARSEABLE")

    def test_malformed_json_cannot_produce_match(self):
        self.write_manifest("{ this is not json ")
        rows, _ = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "UNPARSEABLE")

    def test_real_content_change_is_still_reported(self):
        """Strictness must not mask an ordinary, genuine difference."""
        parsed = json.loads(self.read_manifest())
        parsed["timeZone"] = "Etc/UTC-mutated"
        self.write_manifest(json.dumps(parsed))
        rows, _ = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "MISMATCH")

    def test_absent_file_is_reported_not_omitted(self):
        os.remove(self.manifest)
        rows, mismatches = baseline_verify.verify(self.dir)
        self.assertEqual(status_of(rows, "appsscript"), "ABSENT")
        self.assertGreater(mismatches, 0)

    # -- the answer must not depend on where you stand ----------------------

    def test_same_answer_from_any_working_directory(self):
        script = os.path.join(SCRIPTS, "baseline_verify.py")
        outputs = []
        for cwd in (REPO, SCRIPTS, tempfile.gettempdir()):
            proc = subprocess.run([sys.executable, script], cwd=cwd,
                                  capture_output=True, text=True)
            outputs.append(proc.stdout)
        self.assertEqual(len(set(outputs)), 1,
                         "verifier output depends on the caller's cwd")
        self.assertIn("of 7 stems match deployed v31", outputs[0])

    def test_missing_dir_refuses_to_report_a_count(self):
        script = os.path.join(SCRIPTS, "baseline_verify.py")
        proc = subprocess.run(
            [sys.executable, script, os.path.join(self.tmp, "nope")],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 2)
        self.assertNotIn("stems match", proc.stdout)


if __name__ == "__main__":
    unittest.main()
