"""governor_live_check reads the served version and compares text, offline."""
import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("glc", os.path.join(ROOT, "scripts", "governor_live_check.py"))
glc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(glc)

DEPLOYMENTS = """Found 3 deployments.
- AKfycbwHEAD @HEAD
- AKfycbx0D-5DAnMqOm9YbN3iKDwuiBApEi_xex60f6pwdvObEyQBF5jcOK715pl1mN-Nzn6gng @68 - v68 no names in replies
- AKfycbOTHER @12 - old
"""


class LiveCheck(unittest.TestCase):
    def test_reads_the_version_the_public_deployment_serves(self):
        self.assertEqual(glc.deployed_version(DEPLOYMENTS, glc.DEFAULT_DEPLOYMENT), 68)

    def test_an_unknown_deployment_cannot_be_judged(self):
        with self.assertRaises(SystemExit) as cm:
            glc.deployed_version(DEPLOYMENTS, "AKfycbMISSING")
        self.assertEqual(cm.exception.code, 2)

    def test_line_endings_and_a_trailing_newline_do_not_count_as_drift(self):
        self.assertEqual(glc.norm("a\r\nb\r\n"), glc.norm("a\nb"))

    def test_a_real_change_does(self):
        self.assertNotEqual(glc.norm("var cap = 400;\n"), glc.norm("var cap = 650;\n"))


if __name__ == "__main__":
    unittest.main()
