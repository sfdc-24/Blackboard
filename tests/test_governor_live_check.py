"""governor_live_check: offline, with fake clasp and git.

Codex review of #227: the first version compared only the files present in the
live pull (a main-only file, or an empty pull, still said live == main),
ignored a failed git fetch (a stale origin/main looked current), matched the
deployment id as a suffix, and echoed raw clasp output.
"""
import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("glc", os.path.join(ROOT, "scripts", "governor_live_check.py"))
glc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(glc)

ID = glc.DEFAULT_DEPLOYMENT
DEPLOYMENTS = "Found 2 deployments.\n- AKfycbwHEAD @HEAD\n- %s @68 - v68\n" % ID
MAIN = {"Code.js": "var cap = 400;\n", "Auth.js": "auth\n", "appsscript.json": "{}\n"}


def run(live_files, main_files=MAIN, deployments=DEPLOYMENTS):
    """Run main() against fakes; return (exit code, printed text)."""
    project = tempfile.mkdtemp()
    Path(project, ".clasp.json").write_text('{"scriptId": "x", "rootDir": "."}', encoding="utf-8")

    def fake_clasp(args, cwd):
        if args[0] == "deployments":
            return deployments
        for name, body in live_files.items():
            target = Path(cwd, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return ""

    out = io.StringIO()
    with mock.patch.object(glc, "clasp", fake_clasp), \
         mock.patch.object(glc, "fresh_main", lambda: "a" * 40), \
         mock.patch.object(glc, "main_files", lambda sha: sorted(main_files)), \
         mock.patch.object(glc, "main_source", lambda sha, n: main_files.get(n)), \
         mock.patch.object(sys, "argv", ["x", "--project-dir", project]), \
         contextlib.redirect_stdout(out):
        try:
            code = glc.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue()


class LiveCheck(unittest.TestCase):
    def test_everything_matching_is_exit_0(self):
        code, text = run(dict(MAIN))
        self.assertEqual(code, 0, text)
        self.assertIn("live == main", text)

    def test_a_file_only_on_main_is_drift(self):
        main = dict(MAIN, **{"Safety.js": "guard\n"})
        code, text = run(dict(MAIN), main_files=main)
        self.assertEqual(code, 1, text)
        self.assertIn("Safety.js", text)
        self.assertIn("main only", text)

    def test_a_file_only_live_is_drift(self):
        code, text = run(dict(MAIN, **{"Extra.js": "x\n"}))
        self.assertEqual(code, 1, text)

    def test_an_empty_pull_cannot_be_judged(self):
        code, text = run({})
        self.assertEqual(code, 2, text)

    def test_a_real_change_is_drift_and_line_endings_are_not(self):
        live = dict(MAIN, **{"Code.js": "var cap = 400;\r\n"})
        self.assertEqual(run(live)[0], 0)
        live = dict(MAIN, **{"Code.js": "var cap = 650;\n"})
        self.assertEqual(run(live)[0], 1)

    def test_a_failed_fetch_is_unknown_not_a_match(self):
        with mock.patch.object(glc.subprocess, "run", lambda *a, **k: mock.Mock(returncode=128)):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
                glc.fresh_main()
        self.assertEqual(cm.exception.code, 2)

    def test_the_deployment_id_must_match_as_a_whole_token(self):
        longer = "- X%s @12 - someone else's\n" % ID
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            glc.deployed_version("Found 1 deployment.\n" + longer, ID)
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(glc.deployed_version(DEPLOYMENTS, ID), 68)

    def test_clasp_errors_are_never_echoed(self):
        secretish = "token=abc123 account=someone@example.com"
        with mock.patch.object(glc.shutil, "which", lambda n: "clasp"), \
             mock.patch.object(glc.subprocess, "run", lambda *a, **k: mock.Mock(returncode=1, stdout=secretish, stderr=secretish)):
            out = io.StringIO()
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
                glc.clasp(["deployments"], Path("."))
        self.assertNotIn("abc123", out.getvalue())
        self.assertNotIn("example.com", out.getvalue())


class NestedAndExactIds(unittest.TestCase):
    """Codex's second review of #227: live subdirectories were discarded and
    main was listed non-recursively, and a hyphenated prefix still matched."""

    def test_a_nested_file_only_live_is_drift(self):
        code, text = run(dict(MAIN, **{"guards/Safety.js": "guard\n"}))
        self.assertEqual(code, 1, text)
        self.assertIn("guards/Safety.js", text)

    def test_equal_nested_trees_match(self):
        tree = dict(MAIN, **{"guards/Nested.js": "n\n"})
        code, text = run(dict(tree), main_files=dict(tree))
        self.assertEqual(code, 0, text)

    def test_a_name_with_a_space_is_one_file(self):
        tree = dict(MAIN, **{"Safety Rules.js": "r\n"})
        code, text = run(dict(tree), main_files=dict(tree))
        self.assertEqual(code, 0, text)

    def test_a_hyphenated_longer_id_does_not_answer_for_ours(self):
        listing = "Found 2 deployments.\n- X-%s @12 - other\n- %s @68 - ours\n" % (ID, ID)
        self.assertEqual(glc.deployed_version(listing, ID), 68)

    def test_main_files_are_listed_recursively_with_spaces_kept(self):
        seen = {}

        def fake_check_output(args, **kwargs):
            seen["args"] = args
            return "Code.js\0Safety Rules.js\0guards/Nested.js\0"
        with mock.patch.object(glc.subprocess, "check_output", fake_check_output):
            names = glc.main_files("a" * 40)
        self.assertIn("-r", seen["args"])
        self.assertIn("-z", seen["args"])
        self.assertEqual(names, ["Code.js", "Safety Rules.js", "guards/Nested.js"])

    def test_live_files_are_walked_recursively(self):
        root = Path(tempfile.mkdtemp())
        (root / "guards").mkdir()
        (root / "guards" / "Nested.js").write_text("n", encoding="utf-8")
        (root / "Code.js").write_text("c", encoding="utf-8")
        (root / ".clasp.json").write_text("{}", encoding="utf-8")
        self.assertEqual(sorted(glc.live_files(root)), ["Code.js", "guards/Nested.js"])

if __name__ == "__main__":
    unittest.main()
