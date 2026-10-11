"""What a request may name through the fleet's tool and through a waker's REDIS-OP line.

ONE RULE, ONE FILE, stdlib only, copied into every image that carries either caller (Grok on #367,
item 1, 2026-10-11). This is a filter on what a model's line or a command line may NAME; the access
list, applied by the worker, decides, and is narrower for some principals (milestones-sync may name
any proj: key here and is refused outside proj:milestones: there). It is narrower than the list where
it matters: the list grants every principal read `*` (the owner's 2026-10-08 instruction), and a
read result lands on the board as a RESULT row, so a request for another agent's inbox or the audit
stream would copy it onto the board. The list itself is unchanged; a tool that writes the row cells
directly is under the list alone.

- a SHARED key: the conference channel (conf:) and the projects (proj:), shared by design;
- the agent's OWN inbox: fleet:inbox:<canonical agent id>, one per agent (#359), the alias map of
  the access list resolving aya to chatgpt-codex-desktop where the list is present;
- the worker's own heartbeat, for a read (docs/REDIS-ACCESS.md, PY-02).
Nothing else.
"""
from __future__ import annotations

import json
import os

SHARED_PREFIXES = ("conf:", "proj:")
OWN_INBOX_PREFIX = "fleet:inbox:"
SHARED_READ_KEYS = ("gov:worker:heartbeat",)
READ_OPS = ("get", "hgetall", "xrange")
ACL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "redis_acl.json")


def aliases(path: str = ACL_FILE) -> dict:
    """The access list's alias map, or {} where the file is absent (an image that copies this module
    without the list, the board-watcher's). Then a tag names its own inbox under the tag as given: a
    canonical tag is unchanged; an alias (aya) may name fleet:inbox:aya, which is nobody's inbox, and
    not fleet:inbox:chatgpt-codex-desktop, which is its own. Nothing of another agent's opens."""
    try:
        with open(path, encoding="utf-8") as fh:
            return dict(json.load(fh).get("aliases") or {})
    except (OSError, ValueError):
        return {}


def key_allowed_for(tag: str, op: str, key: str) -> str:
    """"" when `tag` may name `key` with `op`, else the reason it may not (never echoing the key)."""
    tag, key, op = (tag or "").strip().lower(), (key or "").strip(), (op or "").strip().lower()
    if key.startswith(SHARED_PREFIXES):
        return ""
    own = OWN_INBOX_PREFIX + aliases().get(tag, tag)
    if key == own or key.startswith(own + ":"):
        return ""
    if key in SHARED_READ_KEYS and op in READ_OPS:
        return ""
    return ("a request may name a shared key (%s), your own inbox (%s<you>) or %s for a read; nothing "
            "else through this tool" % (", ".join(SHARED_PREFIXES), OWN_INBOX_PREFIX, ", ".join(SHARED_READ_KEYS)))
