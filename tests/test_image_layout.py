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


if __name__ == "__main__":
    unittest.main(verbosity=2)
