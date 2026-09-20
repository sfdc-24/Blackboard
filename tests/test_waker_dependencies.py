#!/usr/bin/env python3
"""agent_waker.py names three modules and shells out to a fourth file.

None of them were on main.

`scripts/agent_waker.py` reached main on 2026-09-20 in PR 154, was extended in
PR 156, and passed every hosted check both times — while `foundry_agent.py`,
`gemini_agent.py` and `fleet_agent.py` existed ONLY on a session branch in one
laptop's working tree. A clean checkout of main could not have run the waker at
all: `__import__(cfg["module"])` raises ImportError on the first line of main(),
and `post_reply` shells out to a `scripts/fleet_agent.py` that is not there.

It went unnoticed because tests/test_agent_waker.py injects fake adapters into
sys.modules on purpose — the right thing for testing addressing and budget, and
exactly why it cannot notice a missing dependency. Green meant the logic was
right, never that the program could start.

Found by moving that working tree onto main and watching three files vanish.

Run: python tests/test_waker_dependencies.py
"""

import importlib
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
sys.path.insert(0, SCRIPTS)

import agent_waker as aw  # noqa: E402


class Dependencies(unittest.TestCase):
    def test_every_registered_adapter_is_importable(self):
        """The check that was missing. No fakes: import the real module."""
        for tag, cfg in sorted(aw.AGENTS.items()):
            with self.subTest(tag=tag):
                name = cfg["module"]
                path = os.path.join(SCRIPTS, name + ".py")
                self.assertTrue(
                    os.path.isfile(path),
                    "%s names module %r, and scripts/%s.py is not in the "
                    "repository. A clean checkout cannot run the %s waker."
                    % (tag, name, name, tag))
                importlib.import_module(name)

    def test_each_adapter_exposes_ask(self):
        """The waker calls mod.ask(prompt[, max_tokens]). Nothing else."""
        for tag, cfg in sorted(aw.AGENTS.items()):
            with self.subTest(tag=tag):
                mod = importlib.import_module(cfg["module"])
                self.assertTrue(callable(getattr(mod, "ask", None)),
                                "%s adapter has no callable ask()" % tag)

    def test_the_poster_it_shells_out_to_exists(self):
        """post_reply runs `python scripts/fleet_agent.py post ...`.

        It is a subprocess, so a missing file is not an ImportError at startup -
        it is every reply silently failing to post, which reads as an agent
        that has nothing to say.
        """
        path = os.path.join(SCRIPTS, "fleet_agent.py")
        self.assertTrue(os.path.isfile(path),
                        "post_reply shells out to scripts/fleet_agent.py")

    def test_importing_an_adapter_needs_no_env_and_no_network(self):
        """These imports run in CI inside an empty network namespace, with no
        .env present. An adapter that reads credentials or calls out at import
        time would fail there, and would also make the waker's own fail-loudly
        startup check useless."""
        for tag, cfg in sorted(aw.AGENTS.items()):
            with self.subTest(tag=tag):
                mod = importlib.import_module(cfg["module"])
                self.assertIsNotNone(mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
