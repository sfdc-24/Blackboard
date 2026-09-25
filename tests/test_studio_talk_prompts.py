"""The talk and recap prompts carry the owner's two asks of 2026-09-25: credit
real context in a few specific words, and open the recap with the goal and
whether it was reached. The existing rules stay."""
from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud" / "studio-controller"))

from workers import talk  # noqa: E402


class Prompts(unittest.TestCase):
    def test_every_talk_agent_credits_real_context_and_still_asks_nothing(self):
        for agent in talk.NAMES:
            system = talk.talk_system(agent)
            self.assertIn("specific compliment", system, agent)
            self.assertIn("never generic praise", system, agent)
            self.assertIn("Do not ask questions", system, agent)
            self.assertIn("at most 30 words", system, agent)
            self.assertTrue(system.rstrip().endswith(talk.USE_POLICY.rstrip()), agent)

    def test_the_recap_opens_with_the_goal_and_keeps_its_limits(self):
        self.assertIn("Open with the goal they came with", talk.RECAP_SYSTEM)
        self.assertIn("whether the session got there", talk.RECAP_SYSTEM)
        self.assertIn("never name or promise any person, price or date", talk.RECAP_SYSTEM)
        self.assertIn("70 at most", talk.RECAP_SYSTEM)


if __name__ == "__main__":
    unittest.main()
