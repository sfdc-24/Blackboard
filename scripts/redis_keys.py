"""The board request's public Redis key and operations.

Source_Tag is a caller-supplied claim. The worker must enforce this rule itself before claiming a
run or touching Redis; the fleet tool and waker apply it earlier for useful feedback. Only the
documented public conference side stream may be read or appended through an unauthenticated board
row. An agent inbox, even one matching the claimed tag, needs an authenticated identity route.
"""
from __future__ import annotations

import datetime
import re

PUBLIC_SIDE_KEY = re.compile(r"^conf:side:[0-9]{8}$")
PUBLIC_SIDE_OPS = ("xadd", "xrange")
BOARD_REDIS_REFUSAL = "Board Redis request refused; only the public shared side stream is available"


def public_side_allowed(op: str, key: str) -> bool:
    """Only the documented conf:side:YYYYMMDD stream, with its stream operations."""
    if (op or "").strip().lower() not in PUBLIC_SIDE_OPS or not PUBLIC_SIDE_KEY.fullmatch(key or ""):
        return False
    try:
        datetime.datetime.strptime(key[-8:], "%Y%m%d")
    except ValueError:
        return False
    return True


def key_allowed_for(tag: str, op: str, key: str) -> str:
    """An empty string for a public board request, otherwise a key-free refusal."""
    return "" if public_side_allowed(op, key) else BOARD_REDIS_REFUSAL
