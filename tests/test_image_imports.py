"""Every image that copies a script also copies every repo module that script imports.

The board-watcher image copied scripts/agent_waker.py but not scripts/repo_context.py, which
agent_waker imports at module level. Every run from 2026-10-08 19:27Z crashed on that import, so no
waker was started for about seven hours and nothing said so. A Dockerfile is a list of files; this
holds it to the import graph of the files it lists.
"""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def local_imports(path: Path) -> set:
    """Names of scripts/*.py modules imported anywhere in this file (module level or inside a function)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return {n for n in names if (SCRIPTS / (n + ".py")).is_file()}


def closure(start: set) -> set:
    seen, todo = set(), list(start)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        todo.extend(local_imports(SCRIPTS / (name + ".py")) - seen)
    return seen


def copied_scripts(dockerfile: Path) -> set:
    text = dockerfile.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\\\n", " ")
    out = set()
    for line in text.splitlines():
        if line.strip().upper().startswith("COPY"):
            out.update(re.findall(r"scripts/([A-Za-z0-9_]+)\.py", line))
    return out


# Imports a script makes only on a path the image never takes, named so the exemption is visible.
NOT_IN_IMAGE = {
    # agent_waker imports the model module named by its config at run time; the watcher only uses the
    # addressing code and never asks a model.
    "board-watcher": {"gemini_agent", "claude_agent", "fleet_agent",
                      # wa_board_outbox imports wa_notify inside its SEND functions; the watcher only
                      # uses its row matching and never sends.
                      "wa_notify"},
}


class ImagesCarryTheirImports(unittest.TestCase):
    def test_every_dockerfile_copies_the_import_closure_of_what_it_copies(self):
        checked = 0
        for dockerfile in sorted((ROOT / "cloud").glob("*/Dockerfile")):
            copied = copied_scripts(dockerfile)
            if not copied:
                continue
            checked += 1
            missing = closure(copied) - copied - NOT_IN_IMAGE.get(dockerfile.parent.name, set())
            self.assertEqual(set(), missing, "%s copies %s but not the modules they import"
                             % (dockerfile.relative_to(ROOT), sorted(copied)))
        self.assertGreater(checked, 3)

    def test_the_board_watcher_has_what_agent_waker_imports(self):
        copied = copied_scripts(ROOT / "cloud" / "board-watcher" / "Dockerfile")
        self.assertIn("repo_context", copied)
        self.assertIn("okf_land", copied)


if __name__ == "__main__":
    unittest.main()
