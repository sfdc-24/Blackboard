"""Projects on the conference canvas: the room's projects, its current one, and the project each ask was made in.

R6a in conference docs/CARDS-ARCHITECTURE.md (Gemini's R6 decisions, 2026-11-04):
  R6.1  live state in Redis; archives as private snapshots, never on the public canvas bucket.
  R6.2  voice only SWITCHES among a small list of existing, active projects with distinct names; new projects come
        from the owner console (R6b, after the gateway is unpinned), never from a heard name.
  R6.3  the bridge stamps the current project on every claim; cards carry it; the canvas shows the current project.
R6b: the owner console creates, switches, archives and clears projects through a `project.command` entry on the
  chair's event stream (call_bridge.py reads it, from a listed owner only): create(), set_current(), archive(),
  clear_current() below. A project's id is slug(name), computed here from the name the console sent; an id already
  taken, in any status, refuses the create (never a rename, never a revival).

Keys (one room each):
  conf:projects:<room>          hash  id -> {"name", "status": "active"|"archived", "created"}
  conf:project:<room>:current   str   id of the current project (absent: no project, everything as before R6)
  gov:callbridge:project:<ask>  str   the project current when the ask was claimed (expires with the ask)

Everything read here is validated and fails closed: an unreadable or malformed entry is no project, never a guess.
"""
from __future__ import annotations

import datetime
import json
import re

PROJECTS_KEY = "conf:projects:%s"
CURRENT_KEY = "conf:project:%s:current"
STAMP_KEY = "gov:callbridge:project:%s"
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 '\-]{0,39}$")      # the same shape as a card's name
STATUSES = ("active", "archived")
VOICE_MAX = 8            # voice switching is for a SMALL list (R6.2); with more, use the console

# "Switch to the Acme project", "let's work on project Acme", "open the Acme project", "go to project Acme".
# The word "project" is required, so ordinary talk ("switch to Grok", "move to the next item") never switches.
_SWITCH = re.compile(
    r"(?i)\b(?:switch(?:\s+over)?|move|change|go|jump|let['’]?s\s+(?:work\s+on|go\s+to|switch\s+to)|open|"
    r"work\s+on|back)\s+(?:to\s+|on\s+)?(?:the\s+)?(?:(?:project\s+(?P<after>[A-Za-z0-9][A-Za-z0-9 '\-]{0,39}?))|"
    r"(?P<before>[A-Za-z0-9][A-Za-z0-9 '\-]{0,39}?)\s+project)\s*[.!?]*\s*$")


# "Don't switch to the Acme project", "no need to open project Acme": a negation before the switch phrase (Codex's
# HOLD on c7da999) means no switch.
_NEGATED = re.compile(r"(?i)\b(?:don['’]?t|do\s+not|not|never|no|won['’]?t|stop|cancel|without)\b")


def _text(value):
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def slug(name: str) -> str:
    """A project id from its name: lower case, words joined by hyphens ("Acme Rollout" -> "acme-rollout")."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return "-".join(words)[:40].strip("-")


def _words(name: str) -> tuple:
    return tuple(re.findall(r"[a-z0-9]+", (name or "").lower()))


def read_projects(conn, room: str) -> dict:
    """{id: {"name", "status", "created"}} for the room; malformed entries are left out."""
    found = {}
    for raw_id, raw in (conn.hgetall(PROJECTS_KEY % room) or {}).items():
        pid = _text(raw_id)
        try:
            info = json.loads(_text(raw))
        except (TypeError, ValueError):
            continue
        if not (isinstance(info, dict) and PROJECT_ID.fullmatch(pid or "")
                and isinstance(info.get("name"), str) and PROJECT_NAME.fullmatch(info["name"])
                and info.get("status") in STATUSES):
            continue
        found[pid] = {"name": info["name"], "status": info["status"], "created": str(info.get("created") or "")}
    return found


def current(conn, room: str):
    """(id, name) of the room's current project when it exists and is active, else None."""
    pid = _text(conn.get(CURRENT_KEY % room))
    if not pid:
        return None
    info = read_projects(conn, room).get(pid)
    if not info or info["status"] != "active":
        return None
    return pid, info["name"]


def stamp(conn, ask: str, room: str) -> str:
    """Record the room's current project for an ask the FIRST time it is seen, and never change it (Codex's HOLD on
    d519e34: a replay after a switch re-stamped an Acme ask as Beta, moving its untagged cards). "No project" is
    recorded too, as "", so a replay cannot give a project to an ask made without one. The ask's project: the id,
    or "" for none.

    NO EXPIRY (Codex's HOLD on 6b8750a): the lifecycle keeps an ask's task record without a TTL and still shows its
    DONE card after MAX_AGE, so the project binding must outlive MAX_AGE too, or a finished card would leave its
    project's canvas and archive once the stamp lapsed. It is one short string per ask, like the task record."""
    now = current(conn, room)
    conn.set(STAMP_KEY % ask, now[0] if now else "", nx=True)
    return stamped(conn, ask)


def stamped(conn, ask: str) -> str:
    """The project an ask was made in, or "" (none, unreadable, or malformed)."""
    pid = _text(conn.get(STAMP_KEY % ask)) or ""
    return pid if PROJECT_ID.fullmatch(pid) else ""


def switch_target(said: str, projects: dict):
    """The id of the ACTIVE project his words switch to, or None. Whole-name match only, among at most VOICE_MAX
    active projects whose names are distinct as words; anything ambiguous switches nothing (R6.2)."""
    said = (said or "").strip()
    match = _SWITCH.search(said)
    if not match or _NEGATED.search(said[:match.start()]):
        return None
    heard = _words(match.group("after") or match.group("before"))
    active = {pid: _words(info["name"]) for pid, info in projects.items() if info["status"] == "active"}
    if not heard or not active or len(active) > VOICE_MAX or len(set(active.values())) != len(active):
        return None
    hits = [pid for pid, words in active.items() if words == heard]
    return hits[0] if len(hits) == 1 else None


def set_current(conn, room: str, pid: str) -> bool:
    """Make an active project current. False (nothing changed) for an unknown or archived project."""
    info = read_projects(conn, room).get(pid)
    if not info or info["status"] != "active":
        return False
    conn.set(CURRENT_KEY % room, pid)
    return True


def _iso(now) -> str:
    """`now` as ISO UTC to the second ("2026-11-04T15:29:00Z"); a naive datetime is taken as UTC."""
    if now.tzinfo is not None:
        now = now.astimezone(datetime.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def create(conn, room: str, name: str, now):
    """A new ACTIVE project from the owner console (R6.2: never from a heard name): its id, or None.

    The id is slug(name), computed here; whatever id the console sent along is not consulted. Nothing is written for a
    name outside PROJECT_NAME, a slug outside PROJECT_ID, or an id the room's hash already holds in ANY status (active,
    archived, or malformed): HSETNX takes the id or refuses it in one step, so a repeat or a race never renames or
    revives a project. The entry is the shape read_projects() accepts."""
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name):
        return None
    pid = slug(name)
    if not PROJECT_ID.fullmatch(pid):
        return None
    entry = json.dumps({"name": name, "status": "active", "created": _iso(now)}, sort_keys=True)
    if not conn.hsetnx(PROJECTS_KEY % room, pid, entry):
        return None
    return pid


def archive(conn, room: str, pid: str) -> bool:
    """Retire an active project: status "archived", and if it was current, no project is current (the canvas goes back
    to its untagged cards). False (nothing changed) for an unknown, malformed or already archived project."""
    info = read_projects(conn, room).get(pid)
    if not info or info["status"] != "active":
        return False
    conn.hset(PROJECTS_KEY % room, pid, json.dumps(dict(info, status="archived"), sort_keys=True))
    if _text(conn.get(CURRENT_KEY % room)) == pid:
        conn.delete(CURRENT_KEY % room)
    return True


def clear_current(conn, room: str) -> bool:
    """No project is current (every untagged card shows again). True when one was set."""
    return bool(conn.delete(CURRENT_KEY % room))
