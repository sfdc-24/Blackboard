"""Every entrypoint in the bus-reconciler image must import from the FLAT layout the image has.

TWO FAILURES IN ONE EVENING, BOTH INVISIBLE TO EVERY OTHER SUITE.

    A MISSING COPY. scripts/append.py gained `import signature`, and the Dockerfile copied
    append.py without copying signature.py. Every test passed, because a test run from scripts/
    sees the whole directory. The image does not: WORKDIR is /app and each file is copied in
    individually, so a module nobody listed is simply absent and the worker dies on import.

    A SYNTAX ERROR IN THREE FILES AT ONCE. Centralising the status keys left
    `for k in (*redis_dual.STATUS_FOR_LOG)` behind - a starred expression with no comma, which is a
    SyntaxError. The unit suites never import the entrypoints, so all 61 of them passed while three
    of the four deployed jobs could not start.

A Cloud Run job that fails at import is the most expensive kind of broken: it costs a build, a
deploy and an execution to discover, and the failure looks like infrastructure. This reproduces the
image's layout from the Dockerfile itself - so adding a COPY line is enough to keep it honest, and
forgetting one fails here in milliseconds instead of in us-central1.

No network. No redis library needed: redis_dual imports it lazily, which is what makes this
possible at all.
"""
import fnmatch
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "cloud" / "bus-reconciler" / "Dockerfile"

# Everything the image is expected to be able to import, entrypoints first.
MODULES = ("serve_requests", "main", "probe", "keyspace_view", "roster_seed",
           "bus_request", "bus_reconcile", "append", "signature", "redis_dual", "bus")


def copied_paths():
    """The repository paths the Dockerfile COPYs, read from the Dockerfile rather than listed here.

    Read rather than duplicated on purpose: a hardcoded list would be a second description of the
    image that can disagree with the first, which is the shape of the bug this file exists to catch.
    """
    out = []
    for line in DOCKERFILE.read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            parts = line.split()
            if len(parts) >= 3:
                out.append(parts[1])
    return out


def in_build_context(path, rules):
    """Whether .gcloudignore lets `path` reach Cloud Build. Last matching rule wins, as git does.

    THE LINK BETWEEN TWO DESCRIPTIONS OF THE IMAGE. Adding the settings file to the Dockerfile was
    not enough: .gcloudignore is an ALLOWLIST, the file was not in it, and the build died with
    "file not found in build context" after a green test run and a merge. The Dockerfile says what
    the image needs; .gcloudignore says what the build may see; nothing compared them.

    A directory rule hides everything under it, so each ancestor is tested too - that is exactly how
    `/scripts/*` re-excludes a file that `!/scripts` had admitted.
    """
    candidates = [path]
    parts = path.split("/")
    for i in range(1, len(parts)):
        candidates.append("/".join(parts[:i]))

    verdict = True                       # nothing matched means nothing excluded it
    for negated, pattern in rules:
        for candidate in candidates:
            if fnmatch.fnmatch(candidate, pattern) or fnmatch.fnmatch(candidate + "/", pattern):
                verdict = negated
                break
    return verdict


def gcloudignore_rules(text):
    """(negated, pattern) in file order, leading slashes and comments stripped."""
    rules = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        rules.append((negated, line.lstrip("/").rstrip("/")))
    return rules


class TheBuildContext(unittest.TestCase):
    """Every file the Dockerfile COPYs must be a file the build is allowed to see."""

    def test_every_copy_source_reaches_the_build(self):
        rules = gcloudignore_rules((ROOT / ".gcloudignore").read_text(encoding="utf-8"))
        for rel in copied_paths():
            with self.subTest(path=rel):
                self.assertTrue(in_build_context(rel, rules),
                                "%s is COPYed by the Dockerfile but excluded by .gcloudignore, so "
                                "the build will fail with 'file not found in build context'" % rel)

    def test_the_allowlist_still_refuses_the_env_file(self):
        """The positive control for the matcher. If this ever passes a .env, the test above is
        meaningless and the allowlist has stopped being one."""
        rules = gcloudignore_rules((ROOT / ".gcloudignore").read_text(encoding="utf-8"))
        for secret in (".env", "scripts/.env", "some/path/.env"):
            self.assertFalse(in_build_context(secret, rules), secret)

    def test_every_copy_source_exists_in_the_repository(self):
        for rel in copied_paths():
            self.assertTrue((ROOT / rel).is_file(), "%s is COPYed and does not exist" % rel)


class TheImageLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.app = Path(cls.tmp.name)
        cls.copied = copied_paths()
        for rel in cls.copied:
            src = ROOT / rel
            if src.is_file():
                shutil.copy2(src, cls.app / src.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_dockerfile_copies_something(self):
        """If this ever reads zero, every other test here passes by vacuum."""
        self.assertGreater(len(self.copied), 5, self.copied)

    def test_every_module_imports_from_the_flat_layout(self):
        for name in MODULES:
            with self.subTest(module=name):
                result = subprocess.run([sys.executable, "-c", "import " + name],
                                        cwd=str(self.app), capture_output=True, text=True)
                detail = (result.stderr or "").strip().splitlines()[-1:] or [""]
                self.assertEqual(0, result.returncode,
                                 "%s cannot import inside the image: %s" % (name, detail[0]))

    def test_the_entrypoints_the_jobs_actually_run_are_present(self):
        """The four deployed jobs name these in their --args. A file that is not in the image is a
        job that cannot start, and the job spec is not in this repository to check against."""
        for entry in ("serve_requests.py", "main.py", "probe.py", "keyspace_view.py"):
            self.assertTrue((self.app / entry).is_file(), "%s is not copied into the image" % entry)

    def test_the_roster_travels_with_the_code_that_reads_it(self):
        """append.py refuses every row when the roster is absent - deliberately, a guard that cannot
        run has not passed - so an image with append.py and no roster writes nothing at all."""
        self.assertTrue((self.app / "signature.py").is_file())
        self.assertTrue((self.app / "agent_roster.json").is_file())

    def test_the_settings_file_travels_and_resolves_inside_the_image(self):
        """THE THIRD ONE TONIGHT, and the sharpest. redis_dual.settings.json had never been copied
        into the image, and under the old code nobody could tell: a missing file read as {} and
        REDIS_DUAL_ENABLED in the job's environment turned the dual-run on regardless. Once an
        unreadable file correctly vetoes both switches, the absence stopped the job dead - the first
        execution on the fixed image reported settings_file_readable=false and refused to connect.

        Asserting the COPY is not enough: the path has to RESOLVE from /app, where parents[1] is "/"
        and the repo-shaped path points at /scripts/... which cannot exist."""
        self.assertTrue((self.app / "redis_dual.settings.json").is_file(),
                        "the switches are not in the image")
        probe = ("import redis_dual, sys; s = redis_dual.Settings();"
                 "sys.exit(0 if s.file_ok else 1)")
        result = subprocess.run([sys.executable, "-c", probe], cwd=str(self.app),
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode,
                         "the settings file does not resolve from the flat layout: %s"
                         % (result.stderr or "").strip()[-200:])


if __name__ == "__main__":
    unittest.main(verbosity=2)
